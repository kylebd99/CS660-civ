-- Hex geometry. Axial coordinates (q, r): moving +q goes east, +r goes
-- south-east. The whole game's notion of "where" is these two functions.

SET search_path TO game, public;

-- Number of hexes you must step through to get from one hex to another.
CREATE FUNCTION hex_distance(q1 int, r1 int, q2 int, r2 int)
RETURNS int LANGUAGE sql IMMUTABLE AS $$
  SELECT (abs(q1 - q2) + abs(q1 + r1 - q2 - r2) + abs(r1 - r2)) / 2;
$$;

-- The six hexes touching (_q, _r), whether or not they exist on the map.
CREATE FUNCTION hex_neighbours(_q int, _r int)
RETURNS TABLE (q int, r int) LANGUAGE sql IMMUTABLE AS $$
  SELECT _q + d.dq, _r + d.dr
  FROM (VALUES (1,0), (1,-1), (0,-1), (-1,0), (-1,1), (0,1)) AS d(dq, dr);
$$;

-- Terrain is a pure function of position and world seed, so the same seed
-- always produces the same map and worldgen needs no random state. Two sine
-- octaves give blobby continents rather than per-hex noise.
CREATE FUNCTION terrain_at(_q int, _r int, _seed int)
RETURNS text LANGUAGE sql IMMUTABLE AS $$
  SELECT CASE
    WHEN h < -0.375 THEN 'ocean'      -- thresholds are quantiles of h, so the
    WHEN h <  0.122 THEN 'grass'      -- terrain mix stays stable across seeds:
    WHEN h <  0.515 THEN 'plains'     -- roughly 20/22/24/20/9/5 per cent
    WHEN h <  0.952 THEN 'forest'
    WHEN h <  1.213 THEN 'hills'
    ELSE                 'mountain'
  END
  FROM (SELECT      sin((_q + _seed) * 0.31) * cos((_r - _seed) * 0.27)
             + 0.5 * sin((_q + _r + _seed) * 0.17) AS h) n;
$$;
