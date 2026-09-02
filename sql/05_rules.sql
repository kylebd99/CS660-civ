-- The rules of the game, as four functions. The client calls these and nothing
-- else: it cannot move a unit or grow a city by writing to a table directly,
-- because it does not know how.

SET search_path TO game, public;

-- --------------------------------------------------------------- identity
-- Which civ the current connection is playing. Set it with
--   SELECT set_config('app.civ_id', '2', false);
--
-- The rules below consult this rather than taking the civ as an argument, so a
-- client cannot act for someone else just by naming them. It is a session
-- setting, not an argument, for the same reason a web app reads the session
-- cookie instead of a "user_id" form field.
CREATE FUNCTION current_civ() RETURNS int LANGUAGE plpgsql STABLE AS $$
DECLARE raw text := current_setting('app.civ_id', true);
BEGIN
  IF raw IS NULL OR raw = '' THEN
    RAISE EXCEPTION 'no civ selected for this session'
      USING HINT = 'SELECT set_config(''app.civ_id'', ''1'', false)';
  END IF;
  RETURN raw::int;
END $$;

-- --------------------------------------------------------------- new_game
-- Wipe the world and deal a fresh one. Terrain is a pure function of the seed,
-- so the same seed always produces the same map.
CREATE FUNCTION new_game(_width int DEFAULT 30, _height int DEFAULT 16,
                         _seed int DEFAULT 42, _civs int DEFAULT 1)
RETURNS void LANGUAGE plpgsql AS $$
DECLARE
  -- Curated names come first; past the end of the list a civ gets a generated
  -- one, so the number of players is limited by how much room the map has
  -- rather than by the length of this array.
  names   text[] := ARRAY['Rome', 'Carthage', 'Egypt', 'Persia',
                          'Greece', 'Babylon', 'Nubia', 'Assyria',
                          'Phoenicia', 'Sparta', 'Macedon', 'Gaul'];
  -- Distinguishable ANSI colours, cycled once they run out. Two civs sharing a
  -- colour is ugly but playable; refusing to start is not.
  colours int[]  := ARRAY[203, 75, 179, 114, 220, 141,
                          44, 209, 84, 170, 39, 130];
  cols int; rows int;
  i int; cid int; home bigint; home_x int; home_y int; guard bigint;
BEGIN
  IF _civs < 1 THEN
    RAISE EXCEPTION 'civs must be at least 1';
  END IF;

  -- RESTART IDENTITY matters: without it unit and city ids keep climbing across
  -- resets, and the first unit of a fresh game is #47 instead of #1.
  TRUNCATE civ_tech, unit, city_tile, city, civ, tile, world RESTART IDENTITY CASCADE;

  INSERT INTO world (id, turn, width, height, seed)
  VALUES (1, 1, _width, _height, _seed);

  -- The map is the whole rectangle, origin at the south-west corner.
  INSERT INTO tile (x, y, terrain)
  SELECT x, y, terrain_at(x, y, _seed)
  FROM generate_series(0, _width - 1) AS x,
       generate_series(0, _height - 1) AS y;

  -- Starting positions go on a grid rather than a line, so a large number of
  -- civs does not queue up along the middle row. One civ still lands in the
  -- centre and two still land at the quarter marks, as before.
  cols := ceil(sqrt(_civs))::int;
  rows := ceil(_civs::numeric / cols)::int;

  FOR i IN 1.._civs LOOP
    INSERT INTO civ (name, colour)
    VALUES (COALESCE(names[i], format('Civ %s', i)),
            colours[1 + (i - 1) % array_length(colours, 1)])
    RETURNING civ_id INTO cid;

    -- Aim at this civ's cell of the grid, then snap to the nearest land tile
    -- nobody is standing on.
    SELECT t.tile_id, t.x, t.y INTO home, home_x, home_y
    FROM tile t JOIN terrain te ON te.code = t.terrain
    WHERE te.passable AND NOT EXISTS (SELECT 1 FROM unit u WHERE u.tile_id = t.tile_id)
    ORDER BY distance(t.x, t.y,
                      (_width  * (2 * ((i - 1) % cols) + 1) / (2 * cols))::int,
                      (_height * (2 * ((i - 1) / cols) + 1) / (2 * rows))::int),
             t.x, t.y            -- several tiles tie on distance; pick one
    LIMIT 1;

    -- With enough civs on a small or watery map the land simply runs out.
    IF home IS NULL THEN
      RAISE EXCEPTION 'no free land left to place % civs on a % x % map',
        _civs, _width, _height;
    END IF;

    INSERT INTO unit (civ_id, type, tile_id, hp, moves_left, actions_left)
    SELECT cid, 'settler', home, ut.max_hp, ut.moves, ut.actions
    FROM unit_type ut WHERE ut.code = 'settler';

    -- An escort on any free neighbouring land tile.
    SELECT t.tile_id INTO guard
    FROM neighbours(home_x, home_y) n
    JOIN tile t    ON t.x = n.x AND t.y = n.y
    JOIN terrain te ON te.code = t.terrain
    WHERE te.passable AND NOT EXISTS (SELECT 1 FROM unit u WHERE u.tile_id = t.tile_id)
    ORDER BY t.x, t.y          -- at most eight rows; without this the escort
    LIMIT 1;                   -- landed on a different side between runs

    IF guard IS NOT NULL THEN
      INSERT INTO unit (civ_id, type, tile_id, hp, moves_left, actions_left)
      SELECT cid, 'warrior', guard, ut.max_hp, ut.moves, ut.actions
      FROM unit_type ut WHERE ut.code = 'warrior';
    END IF;
  END LOOP;

  -- Tell the planner what it has just been handed. A freshly filled table has
  -- no statistics at all -- pg_class.reltuples reads -1 -- so the planner
  -- guesses, and on a large map it guesses badly: asking where a handful of
  -- units are standing turns into a hash join over every tile in the world.
  -- Autovacuum would get round to this eventually; a new game cannot wait.
  ANALYZE tile, unit, city, city_tile, civ;
END $$;

-- --------------------------------------------------------------- found_city
-- Turn a settler into a city, which claims the workable tiles around it.
CREATE FUNCTION found_city(_unit int, _name text)
RETURNS int LANGUAGE plpgsql AS $$
DECLARE u record; new_city int;
BEGIN
  -- Ownership first, so acting on someone else's unit says so plainly rather
  -- than failing later for some incidental reason.
  IF NOT EXISTS (SELECT 1 FROM unit
                 WHERE unit_id = _unit AND civ_id = current_civ()) THEN
    RAISE EXCEPTION 'unit % is not yours', _unit;
  END IF;

  SELECT un.unit_id, un.civ_id, un.tile_id, t.x, t.y INTO u
  FROM unit un
  JOIN tile      t  ON t.tile_id = un.tile_id
  JOIN unit_type ut ON ut.code   = un.type
  WHERE un.unit_id = _unit AND ut.founds_cities;

  IF NOT FOUND THEN
    RAISE EXCEPTION 'unit % cannot found cities', _unit;
  END IF;

  -- Founding is an action like any other. The settler is consumed a few lines
  -- below, so what this really enforces is that a unit which has already acted
  -- this turn cannot also found.
  IF NOT EXISTS (SELECT 1 FROM unit WHERE unit_id = _unit AND actions_left >= 1) THEN
    RAISE EXCEPTION 'unit % has no action left this turn', _unit;
  END IF;
  UPDATE unit SET actions_left = actions_left - 1 WHERE unit_id = _unit;

  INSERT INTO city (civ_id, tile_id, name) VALUES (u.civ_id, u.tile_id, _name)
  RETURNING city_id INTO new_city;

  -- Claim every yielding tile within two rings that no other city holds.
  -- ON CONFLICT is doing real work: city_tile.tile_id is UNIQUE, so a tile
  -- already worked by a neighbour is simply skipped.
  INSERT INTO city_tile (city_id, tile_id)
  SELECT new_city, t.tile_id
  FROM tile t JOIN terrain te ON te.code = t.terrain
  WHERE distance(t.x, t.y, u.x, u.y) BETWEEN 1 AND 2
    AND te.food + te.production + te.gold > 0
  ON CONFLICT (tile_id) DO NOTHING;

  DELETE FROM unit WHERE unit_id = _unit;    -- the settler becomes the city
  RETURN new_city;
END $$;

-- --------------------------------------------------------------- move_unit
-- Walk a unit to (_x, _y) if it can afford to get there. Legality is decided
-- by the reachable() query, so the rules of movement live in exactly one place.
CREATE FUNCTION move_unit(_unit int, _x int, _y int)
RETURNS int LANGUAGE plpgsql AS $$
DECLARE dest bigint; spent int;
BEGIN
  IF NOT EXISTS (SELECT 1 FROM unit
                 WHERE unit_id = _unit AND civ_id = current_civ()) THEN
    RAISE EXCEPTION 'unit % is not yours', _unit;
  END IF;

  SELECT rc.tile_id, rc.cost INTO dest, spent
  FROM reachable(_unit) rc WHERE rc.x = _x AND rc.y = _y;

  IF dest IS NULL THEN
    RAISE EXCEPTION 'unit % cannot reach (%, %) this turn', _unit, _x, _y;
  END IF;

  -- If another unit is standing there, unit_one_per_tile rejects this update.
  UPDATE unit SET tile_id = dest, moves_left = moves_left - spent WHERE unit_id = _unit;
  RETURN spent;
END $$;

-- ------------------------------------------------------------- set_research
-- Choose what to research next. The client used to UPDATE civ directly, which
-- meant nothing stopped you picking a tech whose prerequisites you had not met;
-- available_tech already defines what is legal, so ask it.
CREATE FUNCTION set_research(_tech text) RETURNS void LANGUAGE plpgsql AS $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM available_tech
                 WHERE civ_id = current_civ() AND code = _tech) THEN
    RAISE EXCEPTION '% is not available to you', _tech
      USING HINT = 'you already know it, or a prerequisite is missing';
  END IF;

  UPDATE civ SET researching = _tech WHERE civ_id = current_civ();
END $$;

-- --------------------------------------------------------------- buy_unit
-- Pay gold for a unit and put it down within one tile of one of your cities.
--
-- "Within one tile" includes the city square itself, so a city with no room
-- around it can still garrison. The new unit arrives having already spent its
-- turn: no movement, no action. That is what stops you buying a warrior and
-- attacking with it in the same breath, and it is one line to change if you
-- would rather they arrive ready.
CREATE FUNCTION buy_unit(_type text, _x int, _y int)
RETURNS text LANGUAGE plpgsql AS $$
DECLARE
  kind    record;
  purse   int;
  dest    bigint;
  bought  int;
BEGIN
  SELECT * INTO kind FROM unit_type WHERE code = _type;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'there is no unit called %', _type
      USING HINT = 'SELECT * FROM unit_shop';
  END IF;

  IF kind.required_tech IS NOT NULL
     AND NOT EXISTS (SELECT 1 FROM civ_tech
                     WHERE civ_id = current_civ() AND tech = kind.required_tech) THEN
    RAISE EXCEPTION 'a % needs %', _type, kind.required_tech
      USING HINT = 'SELECT * FROM unit_shop WHERE civ_id = current_civ()';
  END IF;

  SELECT gold INTO purse FROM civ WHERE civ_id = current_civ();
  IF purse < kind.cost THEN
    RAISE EXCEPTION 'a % costs %g and you have %g', _type, kind.cost, purse;
  END IF;

  -- The tile has to exist, be walkable, be empty, and be next to a city of
  -- yours. Checked as one query so the error can say which part failed.
  SELECT t.tile_id INTO dest
  FROM tile t
  JOIN terrain te ON te.code = t.terrain
  WHERE t.x = _x AND t.y = _y AND te.passable;

  IF NOT FOUND THEN
    RAISE EXCEPTION '(%, %) is not somewhere a unit can stand', _x, _y;
  END IF;
  IF EXISTS (SELECT 1 FROM unit WHERE tile_id = dest) THEN
    RAISE EXCEPTION '(%, %) is already occupied', _x, _y;
  END IF;
  IF NOT EXISTS (
       SELECT 1 FROM city c
       JOIN tile ct ON ct.tile_id = c.tile_id
       WHERE c.civ_id = current_civ() AND distance(ct.x, ct.y, _x, _y) <= 1) THEN
    RAISE EXCEPTION '(%, %) is not next to a city of yours', _x, _y;
  END IF;

  UPDATE civ SET gold = gold - kind.cost WHERE civ_id = current_civ();

  INSERT INTO unit (civ_id, type, tile_id, hp, moves_left, actions_left)
  VALUES (current_civ(), _type, dest, kind.max_hp, 0, 0)
  RETURNING unit_id INTO bought;

  RETURN format('bought %s %s at (%s, %s) for %sg; %sg left, ready next turn',
                _type, bought, _x, _y, kind.cost, purse - kind.cost);
END $$;

-- ----------------------------------------------------------------- attack
-- Strike the unit standing on (_x, _y) from an adjacent tile.
--
-- Combat is deliberately deterministic: the same fight always plays out the
-- same way, which makes it something you can reason about in a lecture rather
-- than something you have to re-run. The defender takes the attacker's full
-- strength; the attacker takes half the defender's in return, so attacking is
-- worthwhile but not free. Nobody advances into the tile -- one unit per tile
-- is a unique index, and letting the winner move would mean deciding what
-- happens when it has no movement left.
CREATE FUNCTION attack(_unit int, _x int, _y int)
RETURNS text LANGUAGE plpgsql AS $$
DECLARE
  me      record;
  foe     record;
  dealt   int;
  taken   int;
BEGIN
  SELECT u.unit_id, u.type, u.hp, u.actions_left, t.x, t.y, ut.strength
  INTO me
  FROM unit u
  JOIN tile      t  ON t.tile_id = u.tile_id
  JOIN unit_type ut ON ut.code   = u.type
  WHERE u.unit_id = _unit AND u.civ_id = current_civ();

  IF NOT FOUND THEN
    RAISE EXCEPTION 'unit % is not yours', _unit;
  END IF;
  IF me.strength = 0 THEN
    RAISE EXCEPTION 'a % cannot attack', me.type;
  END IF;
  IF me.actions_left < 1 THEN
    RAISE EXCEPTION 'unit % has no action left this turn', _unit;
  END IF;
  IF distance(me.x, me.y, _x, _y) <> 1 THEN
    RAISE EXCEPTION 'unit % is not next to (%, %)', _unit, _x, _y;
  END IF;

  SELECT u.unit_id, u.type, u.hp, u.civ_id, ut.strength
  INTO foe
  FROM unit u
  JOIN tile      t  ON t.tile_id = u.tile_id
  JOIN unit_type ut ON ut.code   = u.type
  WHERE t.x = _x AND t.y = _y;

  IF NOT FOUND THEN
    RAISE EXCEPTION 'nothing to attack at (%, %)', _x, _y;
  END IF;
  IF foe.civ_id = current_civ() THEN
    RAISE EXCEPTION 'unit % is your own', foe.unit_id;
  END IF;

  dealt := me.strength;
  taken := foe.strength / 2;

  UPDATE unit SET actions_left = actions_left - 1 WHERE unit_id = _unit;

  -- hp is CHECK (hp > 0), so a casualty is removed rather than written to zero.
  IF foe.hp <= dealt THEN
    DELETE FROM unit WHERE unit_id = foe.unit_id;
  ELSE
    UPDATE unit SET hp = hp - dealt WHERE unit_id = foe.unit_id;
  END IF;

  IF me.hp <= taken THEN
    DELETE FROM unit WHERE unit_id = _unit;
  ELSIF taken > 0 THEN
    UPDATE unit SET hp = hp - taken WHERE unit_id = _unit;
  END IF;

  RETURN format('%s %s hits %s %s for %s, takes %s%s%s',
                me.type, _unit, foe.type, foe.unit_id, dealt, taken,
                CASE WHEN foe.hp <= dealt THEN '; defender destroyed' ELSE '' END,
                CASE WHEN me.hp <= taken  THEN '; attacker destroyed' ELSE '' END);
END $$;

-- --------------------------------------------------------------- end_turn
-- The entire world advances in six statements. None of them mentions a
-- particular city, unit or civ: each one operates on every row at once.
CREATE FUNCTION end_turn() RETURNS int LANGUAGE plpgsql AS $$
DECLARE next_turn int;
BEGIN
  -- 1. Cities eat, and bank whatever food is left over.
  UPDATE city c
  SET    food_store = GREATEST(0, c.food_store + y.food - c.population * 2)
  FROM   city_yield y WHERE y.city_id = c.city_id;

  -- 2. Cities that have banked enough grow by a citizen. Each new citizen
  --    costs more than the last, so cities slow down as they get big.
  UPDATE city
  SET    population = population + 1,
         food_store = food_store - (10 + population * 5)
  WHERE  food_store >= 10 + population * 5;

  -- 3. Treasury and research, summed over each civ's cities.
  UPDATE civ c
  SET    gold    = c.gold    + t.gold,
         science = c.science + t.production
  FROM  (SELECT civ_id, SUM(gold) AS gold, SUM(production) AS production
         FROM city_yield GROUP BY civ_id) t
  WHERE  t.civ_id = c.civ_id;

  -- 4. Research that has been paid for completes.
  INSERT INTO civ_tech (civ_id, tech, learned_turn)
  SELECT c.civ_id, c.researching, w.turn
  FROM civ c JOIN tech t ON t.code = c.researching CROSS JOIN world w
  WHERE c.science >= t.cost;

  UPDATE civ c SET science = c.science - t.cost, researching = NULL
  FROM tech t WHERE t.code = c.researching AND c.science >= t.cost;

  -- 5. Every unit gets its movement and its action back.
  UPDATE unit u SET moves_left = ut.moves, actions_left = ut.actions
  FROM unit_type ut
  WHERE ut.code = u.type
    AND (u.moves_left <> ut.moves OR u.actions_left <> ut.actions);

  -- 6. The clock advances.
  UPDATE world SET turn = turn + 1 WHERE id = 1 RETURNING turn INTO next_turn;
  RETURN next_turn;
END $$;
