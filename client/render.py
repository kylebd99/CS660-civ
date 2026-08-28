"""Turning rows into characters. Nothing here touches the database.

Screen layout: the map is a rectangle of square tiles, and tile (x, y) is drawn
at column CELL_WIDTH * x. Lines run bottom-up so that +y is north, which is the
only thing tile_at() has to undo.
"""

RESET = "\x1b[0m"
BOLD = "\x1b[1m"
DIM = "\x1b[2m"
INVERT = "\x1b[7m"

# Screen columns per tile. Two makes a tile look roughly square, since terminal
# characters are about twice as tall as they are wide. Set it to 1 for a denser
# map; nothing outside this module needs to know.
CELL_WIDTH = 2


def paint(text, colour=None, *, bold=False, invert=False):
    prefix = ""
    if colour is not None:
        prefix += f"\x1b[38;5;{colour}m"
    if bold:
        prefix += BOLD
    if invert:
        prefix += INVERT
    return f"{prefix}{text}{RESET}" if prefix else text


def tile_at(origin, line, column):
    """Which tile covers (line, column) of the map block.

    The inverse of the placement in draw_map. A tile occupies all CELL_WIDTH of
    its columns even though its glyph is drawn in the first, so every column
    belongs to some tile -- which is what makes clicking anywhere on a tile
    select it rather than only its left half.
    """
    x0, top_y = origin
    return x0 + column // CELL_WIDTH, top_y - line


def frame_corners(origin, wide, high):
    """Captions for the four corners of the frame, naming the tile at each.

    The world and the viewport are both rectangles, so these are the world's
    own corners when the whole map is in frame and the viewport's when it is
    not -- either way they say where you are looking.
    """
    for line in {0, high - 1}:
        for column, side in ((0, "left"), ((wide - 1) * CELL_WIDTH, "right")):
            x, y = tile_at(origin, line, column)
            yield line, column, f"({x},{y})", side


def draw_map(tiles, units, cities, highlight=frozenset(), coords=True,
             window=None):
    """Return (lines, origin) for the map.

    Later layers win: terrain is overdrawn by cities, cities by units. A tile in
    `highlight` (a set of (x, y)) is inverted, which is how the client shows
    where the selected unit could walk.

    `window` is (x_min, x_max, y_min, y_max) in tile coordinates -- the same
    rectangle map_window() selects in SQL. Given one, the frame is exactly that
    size whether or not there is any map inside it, so the view does not change
    shape as you pan off the edge of the world.

    `origin` is everything tile_at() needs to turn a click back into a tile, so
    the placement arithmetic lives in exactly one place.
    """
    cell = {}
    for t in tiles:
        cell[(t["x"], t["y"])] = paint(t["glyph"], t["colour"])
    for c in cities:
        cell[(c["x"], c["y"])] = paint("@", c["colour"], bold=True)
    for u in units:
        cell[(u["x"], u["y"])] = paint(u["glyph"], u["colour"], bold=True)
    for spot in highlight:
        if spot in cell:
            cell[spot] = paint(cell[spot].replace(RESET, ""), None, invert=True)

    if window is not None:
        x0, x1, y0, y1 = window
    elif cell:
        xs, ys = [x for x, _ in cell], [y for _, y in cell]
        x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    else:
        return ["(empty world -- try `n`)"], (0, 0)

    wide, high = x1 - x0 + 1, y1 - y0 + 1

    # Bucket by line in one pass, dropping anything outside the frame. Filling
    # each line by scanning every tile instead would cost lines x tiles.
    by_line = {}
    for (x, y), glyph in cell.items():
        if x0 <= x <= x1 and y0 <= y <= y1:
            by_line.setdefault(y, []).append(((x - x0) * CELL_WIDTH, glyph))

    # One list per line, one entry per screen column. Kept unjoined until the
    # end: an entry is a whole escape-wrapped glyph, so once joined you can no
    # longer address a column by index.
    grid = []
    for y in range(y0, y1 + 1):
        slots = [" "] * (wide * CELL_WIDTH)
        for column, glyph in by_line.get(y, ()):
            slots[column] = glyph
        grid.append(slots)
    grid.reverse()                            # +y now runs up the screen

    origin = (x0, y1)

    # Corner captions sit on top of terrain rather than in a margin -- a
    # rectangle leaves none -- so they are dim, and one is dropped if two would
    # collide on a narrow frame.
    if coords:
        taken = {}
        for line, column, text, side in frame_corners(origin, wide, high):
            start = column if side == "left" else column - len(text) + 1
            stop = start + len(text) - 1
            if start < 0 or stop >= wide * CELL_WIDTH:
                continue
            if any(start <= e and s <= stop for s, e in taken.get(line, ())):
                continue
            taken.setdefault(line, []).append((start, stop))
            for i, char in enumerate(text):
                grid[line][start + i] = paint(char, 244)

    return ["".join(slots) for slots in grid], origin


def draw_status(world, civ, cities):
    """One line of civ-level state, then one line per city."""
    research = civ["researching"] or paint("nothing", 244)
    head = (f"{BOLD}turn {world['turn']}{RESET}  "
            f"{paint(civ['name'], civ['colour'], bold=True)}  "
            f"{civ['gold']}g  {civ['science']} science  researching {research}")
    body = [f"  @ {c['name']:<10} pop {c['population']}  "
            f"+{c['food']}F {c['production']}P {c['gold']}G  "
            f"{DIM}(stored {c['food_store']} food){RESET}"
            for c in cities]
    return [head] + body


def draw_log(entries, keep=4):
    """The last few statements the client sent."""
    return [f"{DIM}  {line}{RESET}" for line in entries[-keep:]]


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
    """Your units, with what each has left to spend this turn."""
    return "  " + "   ".join(
        f"{paint(str(u['unit_id']), u['colour'], bold=True)}:{u['type']}"
        f"({u['x']},{u['y']}) {u['hp']}hp {u['moves_left']}mp {u['actions_left']}ap"
        for u in units) if units else ""


def emit(lines):
    """Draw a frame from the top of the screen.

    Each line is erased to its right rather than clearing the whole screen
    first: at ten frames a second a full clear visibly flickers. The trailing
    erase-below removes anything left by a taller previous frame.
    """
    body = "\n".join(f"{line}\x1b[K" for line in lines)
    print(f"\x1b[H{body}\x1b[J", end="", flush=True)
