"""civ -- a Civ-like whose entire state and rules live in PostgreSQL.

The client does not know the rules of the game. It reads views to find out what
is true, calls functions to change it, and draws the result. Every statement it
sends to change the world is printed under the map, and `:` lets you send your
own. If you want to know how the game works, read sql/, not this file.

    python3 client/civ.py
"""

import re
import shutil
import sys

import psycopg

from db import DB
import input_management as im
import render

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

# Where to look when you have not said: your first city, else your first unit.
VIEW_HOME = """SELECT x, y FROM (
                 SELECT t.x, t.y, 0 AS rank FROM city c
                   JOIN tile t ON t.tile_id = c.tile_id WHERE c.civ_id = %s
                 UNION ALL
                 SELECT t.x, t.y, 1 FROM unit u
                   JOIN tile t ON t.tile_id = u.tile_id WHERE u.civ_id = %s
               ) home ORDER BY rank LIMIT 1"""

UNITS = """SELECT u.unit_id, u.civ_id, u.type, ut.glyph, u.moves_left,
                  u.actions_left, u.hp, t.x, t.y, c.colour
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


def view_centre(db, state):
    """Middle of the view, as a tile (x, y). Unset, it follows your pieces."""
    if state["view"] is None:
        home = db.one(VIEW_HOME, (active_civ(db, state),) * 2)
        state["view"] = (home["x"], home["y"]) if home else (0, 0)
    return state["view"]


def use_civ(db, state, civ_id):
    """Point this session at a civ, on both sides of the wire.

    The database half is what matters: the rules functions read
    current_civ() rather than trusting a civ id passed in with the request, so
    the server decides what you may touch. Telling the client alone would let
    any `:` query act for anyone.
    """
    state["civ_id"] = civ_id
    state["view"] = None        # look at the new player's own territory
    db.call(SET_SESSION_CIV, (str(civ_id),))


def active_civ(db, state):
    """Which civ this terminal is playing. Defaults to the first one, and is
    re-defaulted whenever a new world is dealt.

    Two terminals on the same database are two players.
    """
    if state["civ_id"] is None:
        row = db.one(FIRST_CIV)
        if row and row["civ_id"] is not None:
            use_civ(db, state, row["civ_id"])
    return state["civ_id"]


# ------------------------------------------------------------------ commands
# Each takes (db, state, args) and either calls the database or changes what is
# highlighted. `state` holds only presentation: the game itself is in Postgres.

def cmd_new(db, state, args):
    width = int(args[0]) if args else 30
    height = int(args[1]) if len(args) > 1 else 16
    civs = int(args[2]) if len(args) > 2 else 1
    seed = int(args[3]) if len(args) > 3 else 42
    db.call(NEW_GAME, (width, height, seed, civs))
    state["civ_id"] = None      # whoever you were does not exist any more
    state["view"] = None


def cmd_move(db, state, args):
    unit, x, y = int(args[0]), int(args[1]), int(args[2])
    db.call(MOVE_UNIT, (unit, x, y))


def cmd_buy(db, state, args):
    """`b` alone prices the catalogue; `b <type> <x> <y>` buys one and puts it
    down. Two forms of one command, like `t`."""
    if not args:
        # Locked units are shown too, greyed out with what they need, so the
        # shop doubles as a reason to research something.
        state["note"] = "  " + "   ".join(
            f"{u['code']} {u['cost']}g ({u['moves']}mp str{u['strength']})"
            if u["unlocked"] else
            render.paint(f"{u['code']} needs {u['required_tech']}", 244)
            for u in db.rows(UNIT_SHOP, (active_civ(db, state),)))
        return
    kind, x, y = args[0], int(args[1]), int(args[2])
    state["note"] = f"  {db.call(BUY_UNIT, (kind, x, y))['outcome']}"


def cmd_attack(db, state, args):
    """Strike an adjacent enemy. The outcome comes back as a sentence, because
    the interesting part is what happened, not a number."""
    unit, x, y = int(args[0]), int(args[1]), int(args[2])
    hit = db.call(ATTACK, (unit, x, y))
    state["note"] = f"  {hit['outcome']}"


def cmd_found(db, state, args):
    name = " ".join(args[1:]) or "New City"
    db.call(FOUND_CITY, (int(args[0]), name))


def cmd_research(db, state, args):
    me = active_civ(db, state)
    if not args:
        state["note"] = "  " + "   ".join(
            f"{t['code']} ({t['cost']})" for t in db.rows(AVAILABLE_TECH, (me,)))
        return
    db.call(SET_RESEARCH, (args[0],))


def cmd_reach(db, state, args):
    unit = int(args[0])
    state["highlight"] = {(row["x"], row["y"]) for row in db.rows(REACHABLE, (unit,))}
    state["log"].append(db.rendered(REACHABLE, (unit,)))


def cmd_end(db, state, args):
    db.call(END_TURN)


def cmd_sql(db, state, args):
    """Send arbitrary SQL to the live game. This is the whole pitch: the world
    is a database, so anything you can express you can do."""
    sql = " ".join(args)
    rows = db.rows(sql)
    state["log"].append(sql)
    state["note"] = render.format_rows(rows)


def cmd_view(db, state, args):
    """Centre the view on a tile. `v` alone goes back to following your pieces,
    which is also what shift+arrow panning steps away from."""
    if not args:
        state["view"] = None
        return
    state["view"] = (int(args[0]), int(args[1]))


def cmd_play_as(db, state, args):
    """Choose which civ this terminal plays. With no argument, list them."""
    civs = db.rows(ALL_CIVS)
    if not args:
        me = active_civ(db, state)
        state["note"] = "  " + "   ".join(
            ("* " if c["civ_id"] == me else "  ")
            + f"{c['civ_id']}:" + render.paint(c["name"], c["colour"], bold=True)
            for c in civs)
        return
    wanted = int(args[0])
    if wanted not in {c["civ_id"] for c in civs}:
        state["note"] = f"  no civ {wanted} -- press `p` to list them"
        return
    use_civ(db, state, wanted)


def cmd_help(db, state, args):
    state["note"] = "\n".join(f"  {help_text}" for _, help_text in COMMANDS.values())


def cmd_quit(db, state, args):
    state["running"] = False


COMMANDS = {
    "n": (cmd_new,      "n [w] [h] [civs] [seed]   start a new game"),
    "m": (cmd_move,     "m <unit> <x> <y>    move a unit"),
    "a": (cmd_attack,   "a <unit> <x> <y>    attack an adjacent enemy"),
    "b": (cmd_buy,      "b [type x y]        list unit prices, or buy one"),
    "c": (cmd_found,    "c <unit> <name>     found a city with a settler"),
    "t": (cmd_research, "t [tech]            list or choose research"),
    "s": (cmd_reach,    "s <unit>            highlight where a unit can go"),
    "e": (cmd_end,      "e                   end the turn"),
    "p": (cmd_play_as,  "p [civ]             list civs, or play as one"),
    "v": (cmd_view,     "v [x y]             centre the view (shift+arrows pan)"),
    ":": (cmd_sql,      ": <sql>             run SQL against the live game"),
    "?": (cmd_help,     "?                   this list"),
    "q": (cmd_quit,     "q                   quit"),
}


# -------------------------------------------------------------------- screen

def screen(db, state):
    world = db.one(WORLD)
    if world is None:
        # Show the note too: an error from the `n` that just failed is exactly
        # what you need to see here.
        return ["No world yet. Type `n` to start a game, `?` for help.",
                state["note"]]

    me = active_civ(db, state)
    civ = db.one(CIV, (me,))
    units, cities = db.rows(UNITS), db.rows(CITIES)

    # The map draws every civ's pieces, because you can see them standing
    # there; they are told apart by colour. Everything below the map is your
    # own dossier, so it is filtered to the civ you are playing. Hiding rival
    # pieces is fog of war, which is a different question and not built yet.
    mine = lambda rows: [row for row in rows if row["civ_id"] == me]

    status = render.draw_status(world, civ, mine(cities))

    # Size the window to whatever is left of the terminal after the status
    # block, two blank lines, the unit line, a possible note, the log and the
    # prompt.
    columns, rows = shutil.get_terminal_size((80, 24))
    wide = max(10, (columns - 1) // render.CELL_WIDTH)
    high = max(5, rows - (len(status) + 10))
    cx, cy = view_centre(db, state)
    window = (cx - wide // 2, cx + wide // 2, cy - high // 2, cy + high // 2)

    tiles = db.rows(TILES, window)
    map_lines, origin = render.draw_map(tiles, units, cities, state["highlight"],
                                        window=window)

    # Remember where the map landed on screen, so a mouse click can be turned
    # back into a tile. One blank line separates it from the status block.
    state["map_at"] = (len(status) + 1, origin)
    state["map_tiles"] = {(t["x"], t["y"]) for t in tiles}

    return [*status,
            "",
            *map_lines,
            "",
            render.unit_line(mine(units)),
            *([state["note"]] if state["note"] else []),
            *render.draw_log(state["log"])]


def tile_clicked(state, column, row):
    """The tile under a click, or None if it missed the map.

    Mouse reports count from 1 and so do terminal rows, while the frame is a
    list starting at 0.
    """
    if "map_at" not in state:
        return None
    first_line, origin = state["map_at"]
    line = (row - 1) - first_line
    if line < 0:
        return None
    spot = render.tile_at(origin, line, column - 1)
    return spot if spot in state["map_tiles"] else None


def click(db, state, line, column, row):
    """A click either picks one of your units or names a destination for the
    one already picked, leaving a command ready for you to check and send."""
    spot = tile_clicked(state, column, row)
    if spot is None:
        return
    x, y = spot
    unit = db.one(MY_UNIT_AT, (x, y, active_civ(db, state)))
    if unit:
        dispatch(db, state, f"s {unit['unit_id']}")
        line["text"] = f"m {unit['unit_id']} "
    else:
        picked = re.fullmatch(r"m (\d+) ?", line["text"])
        if picked:
            line["text"] = f"m {picked.group(1)} {x} {y}"


def dispatch(db, state, line):
    """Run one typed command."""
    verb, args = line[0], line[1:].split()
    if verb not in COMMANDS:
        state["note"] = f"  unknown command {verb!r} -- press ? for help"
        return
    # `s` is the only command that leaves a highlight behind. Clearing here
    # rather than in each handler means a new command can never forget to,
    # and reachability can never linger over a world that has since moved.
    state["highlight"] = set()
    state["note"] = ""
    try:
        COMMANDS[verb][0](db, state, args)
    except psycopg.Error as exc:
        # Constraint violations and rule errors land here. Showing them is
        # the point: the database refusing an illegal move is a feature.
        state["note"] = f"  {render.paint(str(exc).strip(), 203)}"
    except (ValueError, IndexError):
        state["note"] = f"  usage: {COMMANDS[verb][1]}"


def run_interactive(db, state):
    """Redraw on a timer as well as on input.

    Without this the screen only changes when you press a key, so a rival
    playing in another terminal would appear to do nothing until you typed.
    We keep the half-typed line ourselves and re-echo it after each redraw,
    which is why the terminal has to be in cbreak mode: in the usual
    line-buffered mode the text you are typing lives in the kernel where we
    cannot draw it back.
    """
    line = {"text": "", "at": None, "draft": ""}
    with im.terminal():
        while state["running"]:
            render.emit(screen(db, state) + ["", f"> {line['text']}"])
            for key in im.keys():
                press = im.mouse_press(key)
                if key in ("\x03", "\x04"):        # Ctrl-C, Ctrl-D
                    state["running"] = False
                elif key in im.PAN:
                    dx, dy = im.PAN[key]
                    cx, cy = view_centre(db, state)
                    state["view"] = (cx + dx, cy + dy)
                elif press:
                    click(db, state, line, *press)
                else:
                    done = im.edit_line(line, key, state["history"])
                    if done:
                        if state["history"][-1:] != [done]:
                            state["history"].append(done)
                        dispatch(db, state, done)


def run_scripted(db, state):
    """stdin is a pipe, so there is no one to watch a live screen. Read the
    commands as lines and skip the refresh loop entirely."""
    for line in sys.stdin:
        if not state["running"]:
            break
        render.emit(screen(db, state) + ["", f"> {line.strip()}"])
        if line.strip():
            dispatch(db, state, line.strip())
    render.emit(screen(db, state))


def main():
    state = {"highlight": set(), "log": [], "note": "", "civ_id": None,
             "history": [], "view": None, "running": True}
    try:
        db = DB(echo=state["log"].append)
    except psycopg.OperationalError as exc:
        sys.exit(f"cannot reach the database: {exc}\ntry: docker compose up -d && make reset")

    print("\x1b[2J", end="")                    # one clear, then render.emit() overwrites
    try:
        (run_interactive if sys.stdin.isatty() else run_scripted)(db, state)
    finally:
        print("\x1b[?25h")                      # make sure the cursor is visible


if __name__ == "__main__":
    main()
