-- The rules of the game, as four functions. The client calls these and nothing
-- else: it cannot move a unit or grow a city by writing to a table directly,
-- because it does not know how.

SET search_path TO game, public;

-- --------------------------------------------------------------- new_game
-- Wipe the world and deal a fresh one. Terrain is a pure function of the seed,
-- so the same seed always produces the same map.
CREATE FUNCTION new_game(_radius int DEFAULT 6, _seed int DEFAULT 42, _civs int DEFAULT 1)
RETURNS void LANGUAGE plpgsql AS $$
DECLARE
  names   text[] := ARRAY['Rome', 'Carthage', 'Egypt', 'Persia'];
  colours int[]  := ARRAY[203, 75, 179, 114];
  i int; cid int; home bigint; home_q int; home_r int; guard bigint;
BEGIN
  IF _civs < 1 OR _civs > array_length(names, 1) THEN
    RAISE EXCEPTION 'civs must be between 1 and %', array_length(names, 1);
  END IF;

  -- RESTART IDENTITY matters: without it unit and city ids keep climbing across
  -- resets, and the first unit of a fresh game is #47 instead of #1.
  TRUNCATE civ_tech, unit, city_tile, city, civ, tile, world RESTART IDENTITY CASCADE;

  INSERT INTO world (id, turn, radius, seed) VALUES (1, 1, _radius, _seed);

  -- The map is every hex within _radius of the origin.
  INSERT INTO tile (q, r, terrain)
  SELECT q, r, terrain_at(q, r, _seed)
  FROM generate_series(-_radius, _radius) AS q,
       generate_series(-_radius, _radius) AS r
  WHERE abs(q + r) <= _radius;

  FOR i IN 1.._civs LOOP
    INSERT INTO civ (name, colour) VALUES (names[i], colours[i]) RETURNING civ_id INTO cid;

    -- Spread starting positions evenly around the map, then snap to the
    -- nearest land hex nobody is standing on.
    SELECT t.tile_id, t.q, t.r INTO home, home_q, home_r
    FROM tile t JOIN terrain te ON te.code = t.terrain
    WHERE te.passable AND NOT EXISTS (SELECT 1 FROM unit u WHERE u.tile_id = t.tile_id)
    ORDER BY hex_distance(t.q, t.r,
                          round(_radius * 0.55 * cos(2 * pi() * (i - 1) / _civs))::int,
                          round(_radius * 0.55 * sin(2 * pi() * (i - 1) / _civs))::int)
    LIMIT 1;

    INSERT INTO unit (civ_id, type, tile_id, hp, moves_left)
    SELECT cid, 'settler', home, ut.max_hp, ut.moves FROM unit_type ut WHERE ut.code = 'settler';

    -- An escort on any free neighbouring land hex.
    SELECT t.tile_id INTO guard
    FROM hex_neighbours(home_q, home_r) n
    JOIN tile t    ON t.q = n.q AND t.r = n.r
    JOIN terrain te ON te.code = t.terrain
    WHERE te.passable AND NOT EXISTS (SELECT 1 FROM unit u WHERE u.tile_id = t.tile_id)
    LIMIT 1;

    IF guard IS NOT NULL THEN
      INSERT INTO unit (civ_id, type, tile_id, hp, moves_left)
      SELECT cid, 'warrior', guard, ut.max_hp, ut.moves FROM unit_type ut WHERE ut.code = 'warrior';
    END IF;
  END LOOP;
END $$;

-- --------------------------------------------------------------- found_city
-- Turn a settler into a city, which claims the workable hexes around it.
CREATE FUNCTION found_city(_unit int, _name text)
RETURNS int LANGUAGE plpgsql AS $$
DECLARE u record; new_city int;
BEGIN
  SELECT un.unit_id, un.civ_id, un.tile_id, t.q, t.r INTO u
  FROM unit un
  JOIN tile      t  ON t.tile_id = un.tile_id
  JOIN unit_type ut ON ut.code   = un.type
  WHERE un.unit_id = _unit AND ut.founds_cities;

  IF NOT FOUND THEN
    RAISE EXCEPTION 'unit % does not exist or cannot found cities', _unit;
  END IF;

  INSERT INTO city (civ_id, tile_id, name) VALUES (u.civ_id, u.tile_id, _name)
  RETURNING city_id INTO new_city;

  -- Claim every yielding hex within two rings that no other city holds.
  -- ON CONFLICT is doing real work: city_tile.tile_id is UNIQUE, so a hex
  -- already worked by a neighbour is simply skipped.
  INSERT INTO city_tile (city_id, tile_id)
  SELECT new_city, t.tile_id
  FROM tile t JOIN terrain te ON te.code = t.terrain
  WHERE hex_distance(t.q, t.r, u.q, u.r) BETWEEN 1 AND 2
    AND te.food + te.production + te.gold > 0
  ON CONFLICT (tile_id) DO NOTHING;

  DELETE FROM unit WHERE unit_id = _unit;    -- the settler becomes the city
  RETURN new_city;
END $$;

-- --------------------------------------------------------------- move_unit
-- Walk a unit to (_q, _r) if it can afford to get there. Legality is decided
-- by the reachable() query, so the rules of movement live in exactly one place.
CREATE FUNCTION move_unit(_unit int, _q int, _r int)
RETURNS int LANGUAGE plpgsql AS $$
DECLARE dest bigint; spent int;
BEGIN
  SELECT rc.tile_id, rc.cost INTO dest, spent
  FROM reachable(_unit) rc WHERE rc.q = _q AND rc.r = _r;

  IF dest IS NULL THEN
    RAISE EXCEPTION 'unit % cannot reach (%, %) this turn', _unit, _q, _r;
  END IF;

  -- If another unit is standing there, unit_one_per_tile rejects this update.
  UPDATE unit SET tile_id = dest, moves_left = moves_left - spent WHERE unit_id = _unit;
  RETURN spent;
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

  -- 5. Every unit gets its movement back.
  UPDATE unit u SET moves_left = ut.moves
  FROM unit_type ut WHERE ut.code = u.type AND u.moves_left <> ut.moves;

  -- 6. The clock advances.
  UPDATE world SET turn = turn + 1 WHERE id = 1 RETURNING turn INTO next_turn;
  RETURN next_turn;
END $$;
