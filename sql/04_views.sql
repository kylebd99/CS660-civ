-- Everything the game derives rather than stores. Nothing here is cached: each
-- view recomputes from the base tables every time it is read. That is a
-- deliberate choice for teaching -- when it eventually gets slow, the fix is
-- materialisation, and you will be able to measure exactly what it buys.

SET search_path TO game, public;

-- ------------------------------------------------------------- the economy
-- A city claims a lot of tiles but can only work one per citizen, so it works
-- its best ones. Ranking within each city is a window function; the cut-off
-- depends on that city's population, which is why this is a separate view
-- from the aggregate below.
-- What a given civ gets out of a given terrain, once its techs are counted.
--
-- The base yield comes from terrain, the extras from every terrain_bonus row
-- whose tech that civ has learned. Deriving this rather than storing it means
-- a civ's rates change the instant a tech completes, with nothing to keep in
-- step; the cost is that the join is recomputed whenever it is read.
CREATE VIEW gathering_rate AS
SELECT c.civ_id,
       te.code                                        AS terrain,
       te.food       + COALESCE(SUM(b.food), 0)       AS food,
       te.production + COALESCE(SUM(b.production), 0) AS production,
       te.gold       + COALESCE(SUM(b.gold), 0)       AS gold
FROM civ c
CROSS JOIN terrain te
LEFT JOIN terrain_bonus b
       ON b.terrain = te.code
      AND EXISTS (SELECT 1 FROM civ_tech k
                  WHERE k.civ_id = c.civ_id AND k.tech = b.tech)
GROUP BY c.civ_id, te.code, te.food, te.production, te.gold;

CREATE VIEW city_worked AS
SELECT y.city_id, y.tile_id, y.food, y.production, y.gold
FROM (
  SELECT ct.city_id, ct.tile_id, g.food, g.production, g.gold,
         ROW_NUMBER() OVER (PARTITION BY ct.city_id
                            ORDER BY g.food + g.production + g.gold DESC,
                                     ct.tile_id) AS pick
  FROM city_tile ct
  JOIN city           c  ON c.city_id  = ct.city_id
  JOIN tile           ti ON ti.tile_id = ct.tile_id
  JOIN gathering_rate g  ON g.civ_id   = c.civ_id AND g.terrain = ti.terrain
) y
JOIN city c ON c.city_id = y.city_id
WHERE y.pick <= c.population;

-- The entire economic engine of the game: one aggregate over the tiles each
-- city works. There is no other source of food, production or gold anywhere
-- in the codebase.
CREATE VIEW city_yield AS
SELECT c.city_id,
       c.civ_id,
       4 + COALESCE(SUM(w.food),       0) AS food,        -- 4 from the centre
       1 + COALESCE(SUM(w.production), 0) AS production,  -- 1 from the centre
       2 + COALESCE(SUM(w.gold),       0) AS gold         -- 2 from the centre
FROM      city        c
LEFT JOIN city_worked w ON w.city_id = c.city_id
GROUP BY c.city_id, c.civ_id;

-- ------------------------------------------------------------- the tech tree
-- Techs a civ may begin researching right now: ones it does not already know,
-- for which it knows *every* prerequisite.
--
-- "For every prerequisite p of t, the civ knows p" is universal quantification,
-- which SQL has no direct operator for. The standard encoding is the doubly
-- nested NOT EXISTS below: there is no prerequisite that the civ is missing.
-- In relational algebra this is division.
CREATE VIEW available_tech AS
SELECT c.civ_id, t.code, t.name, t.cost
FROM civ c
CROSS JOIN tech t
WHERE NOT EXISTS (                                  -- ...not already known
        SELECT 1 FROM civ_tech k
        WHERE k.civ_id = c.civ_id AND k.tech = t.code)
  AND NOT EXISTS (                                  -- ...no missing prereq
        SELECT 1 FROM tech_prereq p
        WHERE p.tech = t.code
          AND NOT EXISTS (
                SELECT 1 FROM civ_tech k2
                WHERE k2.civ_id = c.civ_id AND k2.tech = p.requires));

-- ------------------------------------------------------------- the shop
-- What is for sale, per civ, and whether that civ has unlocked it yet.
--
-- Shaped like available_tech: a civ_id column rather than current_civ(), so a
-- psql session can read any civ's shop without first saying who it is playing.
-- Locked rows are listed rather than hidden -- knowing a knight needs Bronze
-- Working is what makes the tech tree worth climbing.
CREATE VIEW unit_shop AS
SELECT c.civ_id, ut.code, ut.cost, ut.max_hp, ut.moves, ut.actions,
       ut.strength, ut.founds_cities, ut.required_tech,
       ut.required_tech IS NULL
         OR EXISTS (SELECT 1 FROM civ_tech k
                    WHERE k.civ_id = c.civ_id AND k.tech = ut.required_tech)
         AS unlocked
FROM civ c CROSS JOIN unit_type ut;

-- ------------------------------------------------------------- the map
-- Flattened map rows, so the client never joins anything.
CREATE VIEW map AS
SELECT ti.tile_id, ti.x, ti.y, ti.terrain,
       te.glyph, te.colour, te.passable, te.move_cost
FROM tile ti JOIN terrain te ON te.code = ti.terrain;

-- The slice of the map a terminal can actually show.
--
-- The rectangle is in tile coordinates. How a tile becomes a screen cell is
-- the renderer's business; the database does not need to know.
--
-- Windowing here rather than in the client is the whole point: a world can
-- have millions of tiles and a terminal can show a few hundred, so redrawing
-- is an index range scan instead of a read of the world.
CREATE FUNCTION map_window(_x_min int, _x_max int, _y_min int, _y_max int)
RETURNS TABLE (x int, y int, glyph text, colour int)
LANGUAGE sql STABLE AS $$
  SELECT t.x, t.y, te.glyph, te.colour
  FROM tile t JOIN terrain te ON te.code = t.terrain
  WHERE t.x BETWEEN _x_min AND _x_max
    AND t.y BETWEEN _y_min AND _y_max;
$$;

-- The same rectangle as map_window, but carrying what each tile would yield
-- *for this civ* rather than what it looks like. Reads gathering_rate, so the
-- numbers move when a tech lands -- research Mining and the hills light up.
CREATE FUNCTION yield_window(_civ int, _x_min int, _x_max int,
                             _y_min int, _y_max int)
RETURNS TABLE (x int, y int, food int, production int, gold int)
LANGUAGE sql STABLE AS $$
  SELECT t.x, t.y, g.food, g.production, g.gold
  FROM tile t
  JOIN gathering_rate g ON g.civ_id = _civ AND g.terrain = t.terrain
  WHERE t.x BETWEEN _x_min AND _x_max
    AND t.y BETWEEN _y_min AND _y_max;
$$;

-- ------------------------------------------------------------- movement
-- Where a unit could walk with the movement it has left, as a recursive walk
-- over tile adjacency accumulating terrain move costs.
CREATE FUNCTION reachable(_unit int)
RETURNS TABLE (tile_id bigint, x int, y int, cost int)
LANGUAGE sql STABLE AS $$
  WITH RECURSIVE walk AS (
    SELECT t.tile_id, t.x, t.y, 0 AS cost, u.moves_left
    FROM unit u JOIN tile t ON t.tile_id = u.tile_id
    WHERE u.unit_id = _unit
  UNION ALL
    SELECT nt.tile_id, nt.x, nt.y, w.cost + te.move_cost, w.moves_left
    FROM walk w
    JOIN neighbours(w.x, w.y) n ON true
    JOIN tile    nt ON nt.x = n.x AND nt.y = n.y
    JOIN terrain te ON te.code = nt.terrain
    WHERE te.passable
      AND w.cost + te.move_cost <= w.moves_left
  )
  SELECT w.tile_id, w.x, w.y, min(w.cost)::int
  FROM walk w GROUP BY w.tile_id, w.x, w.y;
$$;
