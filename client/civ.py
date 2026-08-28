"""civ -- a Civ-like whose entire state and rules live in PostgreSQL.

The client does not know the rules of the game. It reads views to find out what
is true, calls functions to change it, and draws the result. Every statement it
sends to change the world is printed under the map, and `:` lets you send your
own. If you want to know how the game works, read sql/, not this file.

    python3 client/civ.py
"""

import sys
import psycopg

from db import DB
import render

# --------------------------------------------------------------------- reads
# Every query the client makes, in one place. None of them encode game rules:
# the views and functions in sql/ do that.

WORLD = "SELECT turn, radius, seed FROM world"

CIV = """SELECT civ_id, name, colour, gold, science, researching
         FROM civ ORDER BY civ_id LIMIT 1"""

TILES = "SELECT q, r, glyph, colour FROM map"

UNITS = """SELECT u.unit_id, u.type, ut.glyph, u.moves_left, t.q, t.r, c.colour
           FROM unit u
           JOIN unit_type ut ON ut.code    = u.type
           JOIN tile      t  ON t.tile_id  = u.tile_id
           JOIN civ       c  ON c.civ_id   = u.civ_id
           ORDER BY u.unit_id"""

CITIES = """SELECT ci.city_id, ci.name, ci.population, ci.food_store,
                   t.q, t.r, cv.colour, y.food, y.production, y.gold
            FROM city       ci
            JOIN tile       t  ON t.tile_id  = ci.tile_id
            JOIN civ        cv ON cv.civ_id  = ci.civ_id
            JOIN city_yield y  ON y.city_id  = ci.city_id
            ORDER BY ci.city_id"""

AVAILABLE_TECH = """SELECT code, name, cost FROM available_tech
                    WHERE civ_id = %s ORDER BY cost, code"""

REACHABLE = "SELECT q, r, cost FROM reachable(%s)"

# -------------------------------------------------------------------- writes

NEW_GAME = "SELECT new_game(%s, %s, %s)"
MOVE_UNIT = "SELECT move_unit(%s, %s, %s) AS cost"
FOUND_CITY = "SELECT found_city(%s, %s) AS city_id"
SET_RESEARCH = "UPDATE civ SET researching = %s WHERE civ_id = %s"
END_TURN = "SELECT end_turn() AS turn"


# ------------------------------------------------------------------ commands
# Each takes (db, state, args) and either calls the database or changes what is
# highlighted. `state` holds only presentation: the game itself is in Postgres.

def cmd_new(db, state, args):
    radius = int(args[0]) if args else 6
    seed = int(args[1]) if len(args) > 1 else 42
    db.call(NEW_GAME, (radius, seed, 1))
    state["highlight"].clear()


def cmd_move(db, state, args):
    unit, q, r = int(args[0]), int(args[1]), int(args[2])
    db.call(MOVE_UNIT, (unit, q, r))
    state["highlight"].clear()


def cmd_found(db, state, args):
    name = " ".join(args[1:]) or "New City"
    db.call(FOUND_CITY, (int(args[0]), name))


def cmd_research(db, state, args):
    civ = db.one(CIV)
    if not args:
        state["note"] = "  ".join(
            f"{t['code']} ({t['cost']})" for t in db.rows(AVAILABLE_TECH, (civ["civ_id"],)))
        return
    db.call(SET_RESEARCH, (args[0], civ["civ_id"]))


def cmd_reach(db, state, args):
    unit = int(args[0])
    state["highlight"] = {(row["q"], row["r"]) for row in db.rows(REACHABLE, (unit,))}
    state["log"].append(db.rendered(REACHABLE, (unit,)))


def cmd_end(db, state, args):
    db.call(END_TURN)
    state["highlight"].clear()


def cmd_sql(db, state, args):
    """Send arbitrary SQL to the live game. This is the whole pitch: the world
    is a database, so anything you can express you can do."""
    sql = " ".join(args)
    rows = db.rows(sql)
    state["log"].append(sql)
    state["note"] = format_rows(rows)


def cmd_help(db, state, args):
    state["note"] = "\n".join(f"  {help_text}" for _, help_text in COMMANDS.values())


def cmd_quit(db, state, args):
    state["running"] = False


COMMANDS = {
    "n": (cmd_new,      "n [radius] [seed]   start a new game"),
    "m": (cmd_move,     "m <unit> <q> <r>    move a unit"),
    "c": (cmd_found,    "c <unit> <name>     found a city with a settler"),
    "t": (cmd_research, "t [tech]            list or choose research"),
    "s": (cmd_reach,    "s <unit>            highlight where a unit can go"),
    "e": (cmd_end,      "e                   end the turn"),
    ":": (cmd_sql,      ": <sql>             run SQL against the live game"),
    "?": (cmd_help,     "?                   this list"),
    "q": (cmd_quit,     "q                   quit"),
}


# -------------------------------------------------------------------- screen

def format_rows(rows):
    if not rows:
        return "  (no rows)"
    cols = list(rows[0])
    width = {c: max(len(c), *(len(str(r[c])) for r in rows)) for c in cols}
    header = "  " + "  ".join(c.ljust(width[c]) for c in cols)
    rule = "  " + "  ".join("-" * width[c] for c in cols)
    body = ["  " + "  ".join(str(r[c]).ljust(width[c]) for c in cols) for r in rows[:20]]
    return "\n".join([header, rule] + body)


def unit_line(units):
    return "  " + "   ".join(
        f"{render.paint(str(u['unit_id']), u['colour'], bold=True)}:{u['type']}"
        f"({u['q']},{u['r']}) {u['moves_left']}mp" for u in units) if units else ""


def screen(db, state):
    print("\x1b[2J\x1b[H", end="")
    world = db.one(WORLD)
    if world is None:
        print("No world yet. Type `n` to start a game, `?` for help.")
        return
    civ, cities = db.one(CIV), db.rows(CITIES)
    for line in render.draw_status(world, civ, cities):
        print(line)
    print()
    for line in render.draw_map(db.rows(TILES), db.rows(UNITS), cities, state["highlight"]):
        print(line)
    print()
    print(unit_line(db.rows(UNITS)))
    if state["note"]:
        print(state["note"])
        state["note"] = ""
    for line in render.draw_log(state["log"]):
        print(line)


def main():
    state = {"highlight": set(), "log": [], "note": "", "running": True}
    try:
        db = DB(echo=state["log"].append)
    except psycopg.OperationalError as exc:
        sys.exit(f"cannot reach the database: {exc}\ntry: docker compose up -d && make reset")

    while state["running"]:
        screen(db, state)
        try:
            raw = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not raw:
            continue
        verb, args = raw[0], raw[1:].split()
        if verb not in COMMANDS:
            state["note"] = f"  unknown command {verb!r} -- press ? for help"
            continue
        try:
            COMMANDS[verb][0](db, state, args)
        except psycopg.Error as exc:
            # Constraint violations and rule errors land here. Showing them is
            # the point: the database refusing an illegal move is a feature.
            state["note"] = f"  {render.paint(str(exc).strip(), 203)}"
        except (ValueError, IndexError):
            state["note"] = f"  usage: {COMMANDS[verb][1]}"


if __name__ == "__main__":
    main()
