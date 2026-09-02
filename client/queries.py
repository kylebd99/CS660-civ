"""Every SQL statement the client sends, and nothing else.

Kept in its own file rather than at the top of a front-end because "every query
in one place" is a claim the project makes, and a file makes it a stronger claim
than a comment block does. Nothing here imports anything; nothing here encodes
a rule. The rules are in sql/.

Read alongside sql/04_views.sql (what these read) and sql/05_rules.sql (what
these call).
"""

# --------------------------------------------------------------------- reads
# Every query the client makes, in one place. None of them encode game rules:
# the views and functions in sql/ do that.

WORLD = "SELECT turn, width, height, seed FROM world"

CIV = """SELECT civ_id, name, colour, gold, science, researching
         FROM civ WHERE civ_id = %s"""

ALL_CIVS = "SELECT civ_id, name, colour FROM civ ORDER BY civ_id"

FIRST_CIV = "SELECT min(civ_id) AS civ_id FROM civ"

# Only the rectangle of tiles the terminal can show. The same four numbers go
# to draw_map, so the window has one definition rather than two.
TILES = "SELECT x, y, glyph, colour FROM map_window(%s, %s, %s, %s)"

# The same window, but what each tile is worth to you rather than how it looks.
YIELDS = "SELECT x, y, food, production, gold FROM yield_window(%s, %s, %s, %s, %s)"

# Where to look when you have not said: your first city, else your first unit.
# The x, y tiebreak is load-bearing: with two units and no city, `ORDER BY rank`
# alone let the planner pick either one, so the opening view moved between runs.
VIEW_HOME = """SELECT x, y FROM (
                 SELECT t.x, t.y, 0 AS rank FROM city c
                   JOIN tile t ON t.tile_id = c.tile_id WHERE c.civ_id = %s
                 UNION ALL
                 SELECT t.x, t.y, 1 FROM unit u
                   JOIN tile t ON t.tile_id = u.tile_id WHERE u.civ_id = %s
               ) home ORDER BY rank, x, y LIMIT 1"""

UNITS = """SELECT u.unit_id, u.civ_id, u.type, ut.glyph, u.moves_left,
                  u.actions_left, u.hp, ut.max_hp, t.x, t.y, c.colour
           FROM unit u
           JOIN unit_type ut ON ut.code    = u.type
           JOIN tile      t  ON t.tile_id  = u.tile_id
           JOIN civ       c  ON c.civ_id   = u.civ_id
           ORDER BY u.unit_id"""

CITIES = """SELECT ci.city_id, ci.civ_id, ci.name, ci.population, ci.food_store,
                   t.x, t.y, cv.colour, y.food, y.production, y.gold
            FROM city       ci
            JOIN tile       t  ON t.tile_id  = ci.tile_id
            JOIN civ        cv ON cv.civ_id  = ci.civ_id
            JOIN city_yield y  ON y.city_id  = ci.city_id
            ORDER BY ci.city_id"""

AVAILABLE_TECH = """SELECT code, name, cost FROM available_tech
                    WHERE civ_id = %s ORDER BY cost, code"""

REACHABLE = "SELECT x, y, cost FROM reachable(%s)"

# Fetched once rather than per frame: the terrain table does not change while
# a game is running. What each kind of tile is called, and what it looks like.
TERRAIN_LEGEND = """SELECT code, glyph, colour, food, production, gold
                    FROM terrain ORDER BY code"""

UNIT_SHOP = """SELECT code, cost, moves, strength, required_tech, unlocked
               FROM unit_shop WHERE civ_id = %s ORDER BY cost"""

MY_UNIT_AT = """SELECT u.unit_id FROM unit u
                JOIN tile t ON t.tile_id = u.tile_id
                WHERE t.x = %s AND t.y = %s AND u.civ_id = %s"""

# -------------------------------------------------------------------- writes

NEW_GAME = "SELECT new_game(%s, %s, %s, %s)"
MOVE_UNIT = "SELECT move_unit(%s, %s, %s) AS cost"
ATTACK = "SELECT attack(%s, %s, %s) AS outcome"
BUY_UNIT = "SELECT buy_unit(%s, %s, %s) AS outcome"
FOUND_CITY = "SELECT found_city(%s, %s) AS city_id"
SET_RESEARCH = "SELECT set_research(%s)"

# SET cannot take a bind parameter, so the identity goes through set_config.
SET_SESSION_CIV = "SELECT set_config('app.civ_id', %s, false)"

END_TURN = "SELECT end_turn() AS turn"
