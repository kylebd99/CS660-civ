-- Grid geometry. Tiles are a rectangle of squares addressed by (x, y): x runs
-- east, y runs north. The whole game's notion of "where" is these two
-- functions, so changing how the world is shaped means changing this file and
-- nothing else.

SET search_path TO game, public;

-- How many steps from one tile to another, moving in any of eight directions.
-- Diagonals count as one step, which is what makes this max() rather than a
-- sum (that would be four-way movement).
CREATE FUNCTION distance(x1 int, y1 int, x2 int, y2 int)
RETURNS int LANGUAGE sql IMMUTABLE AS $$
  SELECT greatest(abs(x1 - x2), abs(y1 - y2));
$$;

-- The eight tiles touching (_x, _y), whether or not they exist on the map.
CREATE FUNCTION neighbours(_x int, _y int)
RETURNS TABLE (x int, y int) LANGUAGE sql IMMUTABLE AS $$
  SELECT _x + d.dx, _y + d.dy
  FROM (VALUES (1,0), (1,1), (0,1), (-1,1),
               (-1,0), (-1,-1), (0,-1), (1,-1)) AS d(dx, dy);
$$;

-- Terrain is a pure function of position and world seed, so the same seed
-- always produces the same map and worldgen needs no random state. Two sine
-- octaves give blobby continents rather than per-tile noise.
CREATE FUNCTION terrain_at(_x int, _y int, _seed int)
RETURNS text LANGUAGE sql IMMUTABLE AS $$
  SELECT CASE
    WHEN h < -0.485 THEN 'ocean'      -- thresholds are quantiles of h over a
    WHEN h < -0.105 THEN 'grass'      -- large rectangle, so the terrain mix
    WHEN h <  0.279 THEN 'plains'     -- stays near 22/22/22/20/9/5 per cent
    WHEN h <  0.672 THEN 'forest'     -- whatever the seed and map size
    WHEN h <  1.031 THEN 'hills'
    ELSE                 'mountain'
  END
  FROM (SELECT      sin((_x + _seed) * 0.31) * cos((_y - _seed) * 0.27)
             + 0.5 * sin((_x + _y + _seed) * 0.17) AS h) n;
$$;
