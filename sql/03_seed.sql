-- The rulebook: terrain yields, unit stats, and the tech tree.
-- Change a number here and the game's balance changes; no code is involved.

SET search_path TO game, public;

INSERT INTO terrain (code, glyph, colour, food, production, gold, move_cost, passable) VALUES
  ('ocean',    '~', 39,  1, 0, 1, 1, false),
  ('grass',    '.', 41,  2, 0, 0, 1, true ),
  ('plains',   ',', 149, 1, 1, 0, 1, true ),
  ('forest',   '#', 28,  1, 2, 0, 2, true ),
  ('hills',    'n', 137, 0, 3, 0, 2, true ),
  ('mountain', '^', 245, 0, 0, 0, 1, false);

INSERT INTO tech (code, name, cost) VALUES
  ('agriculture',      'Agriculture',      20),
  ('mining',           'Mining',           20),
  ('pottery',          'Pottery',          35),
  ('animal_husbandry', 'Animal Husbandry', 35),
  ('bronze_working',   'Bronze Working',   50),
  ('writing',          'Writing',          60),
  ('the_wheel',        'The Wheel',        60),
  ('currency',         'Currency',         90),
  ('mathematics',      'Mathematics',     120),
  ('philosophy',       'Philosophy',      120);

-- The DAG. currency and mathematics each need two prerequisites, which is what
-- makes "what can I research?" a genuine division rather than a simple join.
INSERT INTO tech_prereq (tech, requires) VALUES
  ('pottery',          'agriculture'),
  ('animal_husbandry', 'agriculture'),
  ('bronze_working',   'mining'),
  ('writing',          'pottery'),
  ('the_wheel',        'animal_husbandry'),
  ('currency',         'bronze_working'),
  ('currency',         'pottery'),
  ('mathematics',      'currency'),
  ('mathematics',      'the_wheel'),
  ('philosophy',       'writing');

-- What each tech is worth on the ground. Not every tech pays out: writing and
-- philosophy buy you other techs, and mathematics unlocks nothing at all yet.
INSERT INTO terrain_bonus (tech, terrain, food, production, gold) VALUES
  ('agriculture',      'grass',  1, 0, 0),
  ('pottery',          'ocean',  1, 0, 0),
  ('animal_husbandry', 'plains', 1, 0, 0),
  ('mining',           'hills',  0, 2, 0),
  ('bronze_working',   'forest', 0, 1, 0),
  ('the_wheel',        'plains', 0, 1, 0),
  ('currency',         'grass',  0, 0, 1),
  ('currency',         'plains', 0, 0, 1);

INSERT INTO unit_type (code, glyph, max_hp, moves, actions, strength, cost,
                       founds_cities, required_tech) VALUES
  ('settler', 's', 10, 2, 1,  0, 40, true,  NULL),   -- strength 0: cannot attack
  ('warrior', 'w', 20, 2, 1,  6, 25, false, NULL),
  ('scout',   'o', 10, 4, 1,  2, 15, false, NULL),
  ('knight',  'k', 30, 3, 1, 10, 60, false, 'bronze_working');
