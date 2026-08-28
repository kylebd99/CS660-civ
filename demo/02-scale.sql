-- Lecture demo: why indexes exist.
--
--   make demo-scale
--
-- Deals a very large world, runs the lookup the renderer does on every frame,
-- then drops the (q, r) index and runs it again. Restores the index at the end.

\timing on

\echo '=== deal a large world (this takes a moment) ==='
SELECT game.new_game(300, 7, 1);
SELECT count(*) AS tiles FROM game.tile;

\echo ''
\echo '=== the lookup the renderer does, with the index ==='
EXPLAIN (ANALYZE, BUFFERS) SELECT * FROM game.tile WHERE q = 12 AND r = -40;

\echo ''
\echo '=== the same lookup with no index ==='
ALTER TABLE game.tile DROP CONSTRAINT tile_q_r_key;
EXPLAIN (ANALYZE, BUFFERS) SELECT * FROM game.tile WHERE q = 12 AND r = -40;

\echo ''
\echo '=== put it back ==='
ALTER TABLE game.tile ADD CONSTRAINT tile_q_r_key UNIQUE (q, r);
EXPLAIN (ANALYZE, BUFFERS) SELECT * FROM game.tile WHERE q = 12 AND r = -40;
