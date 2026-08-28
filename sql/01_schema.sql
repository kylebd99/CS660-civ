-- Every fact the game knows lives in these tables. The client holds no state:
-- it draws what it reads and nothing more. If you want to know what the game
-- is, read this file.
--
-- The map is a hexagon of hexes addressed by axial coordinates (q, r).
-- See sql/02_hex.sql for the two functions that define hex geometry.

DROP SCHEMA IF EXISTS game CASCADE;
CREATE SCHEMA game;
SET search_path TO game, public;

-- ---------------------------------------------------------------- static data
-- These four tables are rules, not state: they are seeded once and never
-- change during play.

CREATE TABLE terrain (
  code       text PRIMARY KEY,
  glyph      text NOT NULL,
  colour     int  NOT NULL,                        -- ANSI 256 colour
  food       int  NOT NULL DEFAULT 0,
  production int  NOT NULL DEFAULT 0,
  gold       int  NOT NULL DEFAULT 0,
  move_cost  int  NOT NULL DEFAULT 1 CHECK (move_cost > 0),
  passable   boolean NOT NULL DEFAULT true
);

CREATE TABLE unit_type (
  code   text PRIMARY KEY,
  glyph  text NOT NULL,
  max_hp int  NOT NULL,
  moves  int  NOT NULL CHECK (moves > 0),
  founds_cities boolean NOT NULL DEFAULT false
);

CREATE TABLE tech (
  code text PRIMARY KEY,
  name text NOT NULL,
  cost int  NOT NULL CHECK (cost > 0)
);

-- Edges of the tech DAG. "tech" cannot be researched until every "requires"
-- row for it is known. Asking which techs that leaves available is relational
-- division -- see the available_tech view in 04_views.sql.
CREATE TABLE tech_prereq (
  tech     text NOT NULL REFERENCES tech,
  requires text NOT NULL REFERENCES tech,
  PRIMARY KEY (tech, requires),
  CHECK (tech <> requires)
);

-- --------------------------------------------------------------------- state

-- One row, always. Holds the clock.
CREATE TABLE world (
  id     int PRIMARY KEY DEFAULT 1 CHECK (id = 1),
  turn   int NOT NULL DEFAULT 1,
  radius int NOT NULL,
  seed   int NOT NULL
);

CREATE TABLE civ (
  civ_id      serial PRIMARY KEY,
  name        text NOT NULL UNIQUE,
  colour      int  NOT NULL,
  gold        int  NOT NULL DEFAULT 0,
  science     int  NOT NULL DEFAULT 0 CHECK (science >= 0),
  researching text REFERENCES tech                 -- NULL = nothing selected
);

CREATE TABLE tile (
  tile_id bigserial PRIMARY KEY,
  q       int  NOT NULL,
  r       int  NOT NULL,
  terrain text NOT NULL REFERENCES terrain,
  -- This is the index the performance demo drops and rebuilds. Every lookup
  -- the renderer and the movement rules do goes through (q, r).
  UNIQUE (q, r)
);

CREATE TABLE city (
  city_id    serial PRIMARY KEY,
  civ_id     int    NOT NULL REFERENCES civ,
  tile_id    bigint NOT NULL UNIQUE REFERENCES tile,   -- one city per tile
  name       text   NOT NULL,
  population int    NOT NULL DEFAULT 1 CHECK (population > 0),
  food_store int    NOT NULL DEFAULT 0 CHECK (food_store >= 0)
);

-- Which tiles each city works. The UNIQUE(tile_id) is the interesting part:
-- it is what stops two cities harvesting the same hex, and it is enforced by
-- the database rather than by any line of application code.
CREATE TABLE city_tile (
  city_id int    NOT NULL REFERENCES city ON DELETE CASCADE,
  tile_id bigint NOT NULL REFERENCES tile,
  PRIMARY KEY (city_id, tile_id),
  UNIQUE (tile_id)
);

CREATE TABLE unit (
  unit_id    serial PRIMARY KEY,
  civ_id     int    NOT NULL REFERENCES civ,
  type       text   NOT NULL REFERENCES unit_type,
  tile_id    bigint NOT NULL REFERENCES tile,
  hp         int    NOT NULL CHECK (hp > 0),
  moves_left int    NOT NULL CHECK (moves_left >= 0)
);

-- At most one unit per hex. Under concurrent turns this is the constraint that
-- turns a silent corruption into a loud, recoverable error.
CREATE UNIQUE INDEX unit_one_per_tile ON unit (tile_id);

CREATE TABLE civ_tech (
  civ_id       int  NOT NULL REFERENCES civ,
  tech         text NOT NULL REFERENCES tech,
  learned_turn int  NOT NULL,
  PRIMARY KEY (civ_id, tech)
);
