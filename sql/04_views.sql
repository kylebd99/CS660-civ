-- Everything the game derives rather than stores. Nothing here is cached: each
-- view recomputes from the base tables every time it is read. That is a
-- deliberate choice for teaching -- when it eventually gets slow, the fix is
-- materialisation, and you will be able to measure exactly what it buys.

SET search_path TO game, public;

-- ------------------------------------------------------------- the economy
-- A city claims a lot of hexes but can only work one per citizen, so it works
-- its best ones. Ranking within each city is a window function; the cut-off
-- depends on that city's population, which is why this is a separate view
-- from the aggregate below.
CREATE VIEW city_worked AS
SELECT r.city_id, r.tile_id, r.food, r.production, r.gold
FROM (
  SELECT ct.city_id, ct.tile_id, te.food, te.production, te.gold,
         ROW_NUMBER() OVER (PARTITION BY ct.city_id
                            ORDER BY te.food + te.production + te.gold DESC,
                                     ct.tile_id) AS pick
  FROM city_tile ct
  JOIN tile    ti ON ti.tile_id = ct.tile_id
  JOIN terrain te ON te.code    = ti.terrain
) r
JOIN city c ON c.city_id = r.city_id
WHERE r.pick <= c.population;

-- The entire economic engine of the game: one aggregate over the tiles each
-- city works. There is no other source of food, production or gold anywhere
-- in the codebase.
CREATE VIEW city_yield AS
SELECT c.city_id,
       c.civ_id,
       4 + COALESCE(SUM(w.food),       0) AS food,        -- 4 from the centre
       1 + COALESCE(SUM(w.production), 0) AS production,  -- 1 from the centre
           COALESCE(SUM(w.gold),       0) AS gold
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

-- ------------------------------------------------------------- the map
-- Flattened map rows for the renderer, so the client never joins anything.
CREATE VIEW map AS
SELECT ti.tile_id, ti.q, ti.r, ti.terrain,
       te.glyph, te.colour, te.passable, te.move_cost
FROM tile ti JOIN terrain te ON te.code = ti.terrain;

-- ------------------------------------------------------------- movement
-- Where a unit could walk with the movement it has left, as a recursive walk
-- over hex adjacency accumulating terrain move costs.
--
-- This is deliberately the naive formulation: it re-expands hexes reached by
-- more than one path and only takes the minimum at the end. With 2-4 movement
-- points that is fine, and it gives you a query whose plan is worth looking at
-- once you get to query optimisation.
CREATE FUNCTION reachable(_unit int)
RETURNS TABLE (tile_id bigint, q int, r int, cost int)
LANGUAGE sql STABLE AS $$
  WITH RECURSIVE walk AS (
    SELECT t.tile_id, t.q, t.r, 0 AS cost, u.moves_left
    FROM unit u JOIN tile t ON t.tile_id = u.tile_id
    WHERE u.unit_id = _unit
  UNION ALL
    SELECT nt.tile_id, nt.q, nt.r, w.cost + te.move_cost, w.moves_left
    FROM walk w
    JOIN hex_neighbours(w.q, w.r) n ON true
    JOIN tile    nt ON nt.q = n.q AND nt.r = n.r
    JOIN terrain te ON te.code = nt.terrain
    WHERE te.passable
      AND w.cost + te.move_cost <= w.moves_left
  )
  SELECT w.tile_id, w.q, w.r, min(w.cost)::int
  FROM walk w GROUP BY w.tile_id, w.q, w.r;
$$;
