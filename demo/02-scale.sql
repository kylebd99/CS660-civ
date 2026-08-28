-- Lecture demo: why indexes exist.
--
--   make demo-scale
--
-- Deals a very large world, runs the lookup the renderer does on every frame,
-- then drops the (x, y) index and runs it again. Restores the index at the end.

\timing on

\echo '=== deal a large world (this takes a moment) ==='
SELECT game.new_game(700, 400, 7, 1);
SELECT count(*) AS tiles FROM game.tile;

\echo ''
\echo '=== the lookup the renderer does, with the index ==='
EXPLAIN (ANALYZE, BUFFERS) SELECT * FROM game.tile WHERE x = 512 AND y = 240;

\echo ''
\echo '=== the same lookup with no index ==='
ALTER TABLE game.tile DROP CONSTRAINT tile_x_y_key;
EXPLAIN (ANALYZE, BUFFERS) SELECT * FROM game.tile WHERE x = 512 AND y = 240;

\echo ''
\echo '=== put it back ==='
ALTER TABLE game.tile ADD CONSTRAINT tile_x_y_key UNIQUE (x, y);
EXPLAIN (ANALYZE, BUFFERS) SELECT * FROM game.tile WHERE x = 512 AND y = 240;
