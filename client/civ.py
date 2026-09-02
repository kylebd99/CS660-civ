"""civ -- the terminal front-end.

The rules are in sql/, the statements in queries.py, and everything any
front-end needs in game.py. What is left here is what only a terminal needs:
turning typed words into calls, turning rows into characters, and the loop.

Every statement sent to change the world is printed under the map, and `:` lets
you send your own. If you want to know how the game works, read sql/, not this
file.

    python3 client/civ.py
"""

import re
import shutil
import sys

import psycopg

import game
import input_management as im
import render
from db import DB

# ------------------------------------------------------------------ commands
# Each takes (db, session, ui, args) and is deliberately thin: parse the words,
# call game, format a note. `session` is what both front-ends hold; `ui` is
# this one's own -- the note under the map, the command history, whether to
# keep going.

def cmd_new(db, session, ui, args):
    game.new_game(db, session,
                  width=int(args[0]) if args else 30,
                  height=int(args[1]) if len(args) > 1 else 16,
                  civs=int(args[2]) if len(args) > 2 else 1,
                  seed=int(args[3]) if len(args) > 3 else 42)


def cmd_move(db, session, ui, args):
    game.move(db, int(args[0]), int(args[1]), int(args[2]))


def cmd_buy(db, session, ui, args):
    """`b` alone prices the catalogue; `b <type> <x> <y>` buys one and puts it
    down. Two forms of one command, like `t`."""
    if not args:
        # Locked units are shown too, greyed out with what they need, so the
        # shop doubles as a reason to research something.
        ui["note"] = "  " + "   ".join(
            f"{u['code']} {u['cost']}g ({u['moves']}mp str{u['strength']})"
            if u["unlocked"] else
            render.paint(f"{u['code']} needs {u['required_tech']}", 244)
            for u in game.unit_shop(db, game.active_civ(db, session)))
        return
    ui["note"] = "  " + game.buy(db, args[0], int(args[1]), int(args[2]))


def cmd_attack(db, session, ui, args):
    """Strike an adjacent enemy. The outcome comes back as a sentence, because
    the interesting part is what happened, not a number."""
    ui["note"] = "  " + game.attack(db, int(args[0]), int(args[1]), int(args[2]))


def cmd_found(db, session, ui, args):
    game.found_city(db, int(args[0]), " ".join(args[1:]) or "New City")


def cmd_research(db, session, ui, args):
    me = game.active_civ(db, session)
    if not args:
        ui["note"] = "  " + "   ".join(
            f"{t['code']} ({t['cost']})" for t in game.available_techs(db, me))
        return
    game.set_research(db, args[0])


def cmd_reach(db, session, ui, args):
    game.highlight_reachable(db, session, int(args[0]))


def cmd_end(db, session, ui, args):
    game.end_turn(db)


def cmd_sql(db, session, ui, args):
    """Send arbitrary SQL to the live game. This is the whole pitch: the world
    is a database, so anything you can express you can do."""
    ui["note"] = render.format_rows(game.run_sql(db, session, " ".join(args)))


def cmd_yield(db, session, ui, args):
    """Colour the map by what tiles are worth instead of what they are.

    `y` on its own goes back to terrain. Red is nothing, green is the best in
    view, and the glyph is the number, so the map can be read either way.
    """
    if not args:
        session.overlay = None
        return
    field = game.OVERLAYS.get(args[0].lower())
    if field is None:
        ui["note"] = ("  show what? "
                      + " ".join(sorted(set(game.OVERLAYS.values())))
                      + "   (`y` alone returns to terrain)")
        return
    session.overlay = field


def cmd_view(db, session, ui, args):
    """Centre the view on a tile. `v` alone goes back to following your pieces,
    which is also what shift+arrow panning steps away from."""
    game.look_at(session, *(int(a) for a in args[:2]))


def cmd_play_as(db, session, ui, args):
    """Choose which civ this terminal plays. With no argument, list them."""
    civs = game.all_civs(db)
    if not args:
        me = game.active_civ(db, session)
        ui["note"] = "  " + "   ".join(
            ("* " if c["civ_id"] == me else "  ")
            + f"{c['civ_id']}:" + render.paint(c["name"], c["colour"], bold=True)
            for c in civs)
        return
    wanted = int(args[0])
    if wanted not in {c["civ_id"] for c in civs}:
        ui["note"] = f"  no civ {wanted} -- press `p` to list them"
        return
    game.use_civ(db, session, wanted)


def cmd_help(db, session, ui, args):
    ui["note"] = "\n".join(f"  {help_text}" for _, help_text in COMMANDS.values())


def cmd_quit(db, session, ui, args):
    ui["running"] = False


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
    "y": (cmd_yield,    "y [food|prod|gold]  colour the map by yield"),
    ":": (cmd_sql,      ": <sql>             run SQL against the live game"),
    "?": (cmd_help,     "?                   this list"),
    "q": (cmd_quit,     "q                   quit"),
}


# -------------------------------------------------------------------- screen

def screen(db, session, ui):
    snap = game.snapshot(db, session)
    if snap is None:
        # Show the note too: an error from the `n` that just failed is exactly
        # what you need to see here.
        return ["No world yet. Type `n` to start a game, `?` for help.",
                ui["note"]]

    # Built once to measure, since the overlay label changes the first line's
    # content but never the line count, and the window needs that count.
    status = render.draw_status(snap["world"], snap["civ"], snap["my_cities"])

    # Size the window to whatever is left of the terminal after the status
    # block, two blank lines, the unit line, a possible note, the log and the
    # prompt.
    columns, rows = shutil.get_terminal_size((80, 24))
    wide = max(10, (columns - 1) // render.CELL_WIDTH)
    high = max(5, rows - (len(status) + 10))
    window = game.window_for(game.view_centre(db, session), wide, high)

    # draw_map is told nothing about overlays: it is handed cells with a glyph
    # and a colour either way, and does not care where they came from.
    cells, highest = game.tiles_in(db, session, window)
    if highest is None:
        tiles = cells
    else:
        tiles = render.heat_cells(cells, highest)
        status = render.draw_status(snap["world"], snap["civ"], snap["my_cities"],
                                    f"showing {session.overlay} 0-{highest}")

    map_lines, origin = render.draw_map(tiles, snap["units"], snap["cities"],
                                        session.highlight, window=window)

    # Remember where the map landed on screen, so a mouse click can be turned
    # back into a tile. One blank line separates it from the status block.
    ui["map_at"] = (len(status) + 1, origin)
    ui["map_tiles"] = {(tile["x"], tile["y"]) for tile in tiles}

    return [*status,
            "",
            *map_lines,
            "",
            render.unit_line(snap["my_units"]),
            *([ui["note"]] if ui["note"] else []),
            *render.draw_log(session.log)]


# ------------------------------------------------------------------ clicking

def tile_clicked(ui, column, row):
    """The tile under a click, or None if it missed the map.

    Mouse reports count from 1 and so do terminal rows, while the frame is a
    list starting at 0.
    """
    if "map_at" not in ui:
        return None
    first_line, origin = ui["map_at"]
    line = (row - 1) - first_line
    if line < 0:
        return None
    spot = render.tile_at(origin, line, column - 1)
    return spot if spot in ui["map_tiles"] else None


def click(db, session, ui, line, column, row):
    """Click one of your units to pick it, then a square to send it there.

    Picking leaves `m <unit> ` in the prompt, which is both the visible sign of
    what is selected and how the second click knows what to move. That second
    click runs the move rather than typing it for you.
    """
    spot = tile_clicked(ui, column, row)
    if spot is None:
        return
    x, y = spot
    unit = game.my_unit_at(db, x, y, game.active_civ(db, session))
    if unit:
        dispatch(db, session, ui, f"s {unit}")
        line["text"] = f"m {unit} "
        return

    picked = re.fullmatch(r"m (\d+) ?", line["text"])
    if not picked:
        return
    dispatch(db, session, ui, f"m {picked.group(1)} {x} {y}")

    # dispatch() clears the note before running, so a note now means the move
    # was refused -- out of reach, or someone standing there. Stay selected and
    # put the reachable squares back so the next click can just be a better
    # one, rather than making you re-pick the unit.
    if ui["note"]:
        line["text"] = f"m {picked.group(1)} "
        session.highlight = game.reachable(db, int(picked.group(1)))
    else:
        line["text"] = ""


# ------------------------------------------------------------------- the loop

def dispatch(db, session, ui, line):
    """Run one typed command."""
    verb, args = line[0], line[1:].split()
    if verb not in COMMANDS:
        ui["note"] = f"  unknown command {verb!r} -- press ? for help"
        return
    # `s` is the only command that leaves a highlight behind. Clearing here
    # rather than in each handler means a new command can never forget to,
    # and reachability can never linger over a world that has since moved.
    game.clear_highlight(session)
    ui["note"] = ""
    try:
        COMMANDS[verb][0](db, session, ui, args)
    except psycopg.Error as exc:
        # Constraint violations and rule errors land here. Showing them is
        # the point: the database refusing an illegal move is a feature.
        ui["note"] = f"  {render.paint(str(exc).strip(), 203)}"
    except (ValueError, IndexError):
        ui["note"] = f"  usage: {COMMANDS[verb][1]}"


def run_interactive(db, session, ui):
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
        while ui["running"]:
            render.emit(screen(db, session, ui) + ["", f"> {line['text']}"])
            for key in im.keys():
                press = im.mouse_press(key)
                if key in ("\x03", "\x04"):        # Ctrl-C, Ctrl-D
                    ui["running"] = False
                elif key in im.PAN:
                    game.pan(db, session, *im.PAN[key])
                elif press:
                    click(db, session, ui, line, *press)
                else:
                    done = im.edit_line(line, im.named(key), ui["history"])
                    if done:
                        if ui["history"][-1:] != [done]:
                            ui["history"].append(done)
                        dispatch(db, session, ui, done)


def run_scripted(db, session, ui):
    """stdin is a pipe, so there is no one to watch a live screen. Read the
    commands as lines and skip the refresh loop entirely."""
    for line in sys.stdin:
        if not ui["running"]:
            break
        render.emit(screen(db, session, ui) + ["", f"> {line.strip()}"])
        if line.strip():
            dispatch(db, session, ui, line.strip())
    render.emit(screen(db, session, ui))


def main():
    session = game.Session()
    ui = {"note": "", "history": [], "running": True}
    try:
        db = DB(echo=session.log.append)
    except psycopg.OperationalError as exc:
        sys.exit(f"cannot reach the database: {exc}\ntry: docker compose up -d && make reset")

    print("\x1b[2J", end="")                    # one clear, then render.emit() overwrites
    try:
        (run_interactive if sys.stdin.isatty() else run_scripted)(db, session, ui)
    finally:
        print("\x1b[?25h")                      # make sure the cursor is visible


if __name__ == "__main__":
    main()
