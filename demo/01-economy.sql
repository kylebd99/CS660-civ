-- Lecture demo: the economy is one query.
--
--   make demo-economy

\echo '=== every city, and what it produces this turn ==='
SELECT * FROM game.city_yield;

\echo ''
\echo '=== why: each city works its best <population> tiles ==='
SELECT c.name, c.population, t.x, t.y, t.terrain, w.food, w.production, w.gold
FROM game.city_worked w
JOIN game.city c ON c.city_id = w.city_id
JOIN game.tile t ON t.tile_id = w.tile_id
ORDER BY c.name, w.food + w.production + w.gold DESC;

\echo ''
\echo '=== and the plan Postgres chose to compute it ==='
EXPLAIN ANALYZE SELECT * FROM game.city_yield;
