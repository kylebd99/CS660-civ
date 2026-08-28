"""Turning rows into characters. Nothing here touches the database.

Screen layout for a hex grid in axial coordinates: a hex at (q, r) is drawn at
column 2q + r and row r. The +r is what staggers each row half a hex to the
right and makes the grid interlock.
"""

RESET = "\x1b[0m"
BOLD = "\x1b[1m"
DIM = "\x1b[2m"
INVERT = "\x1b[7m"


def paint(text, colour=None, *, bold=False, invert=False):
    prefix = ""
    if colour is not None:
        prefix += f"\x1b[38;5;{colour}m"
    if bold:
        prefix += BOLD
    if invert:
        prefix += INVERT
    return f"{prefix}{text}{RESET}" if prefix else text


def corner_labels(cell):
    """Captions for the six points of the hex-shaped map.

    A hexagon of radius R has its corners at (R,0), (0,R), (-R,R), (-R,0),
    (0,-R) and (R,-R). R is recovered from the hexes themselves, so a map that
    is not a full hexagon simply loses the corners it does not have.

    Yields (row, x, text, side); side says which way to write from the corner.
    """
    radius = max(max(abs(q), abs(r), abs(q + r)) for q, r in cell)
    corners = [(radius, 0), (0, radius), (-radius, radius),
               (-radius, 0), (0, -radius), (radius, -radius)]
    for q, r in dict.fromkeys(corners):
        if (q, r) in cell:
            x = 2 * q + r
            yield r, x, f"({q},{r})", "left" if x < 0 else "right"


def draw_map(tiles, units, cities, highlight=frozenset(), coords=True,
             window=None):
    """Return the map as a list of lines.

    Later layers win: terrain is overdrawn by cities, cities by units. A hex in
    `highlight` (a set of (q, r)) is inverted, which is how the client shows
    where the selected unit could walk. With `coords`, the six corners of
    the map are captioned with their axial coordinates.

    `window` is (col_min, col_max, row_min, row_max) in screen cells. Given
    one, the frame is exactly that size whether or not there is any map inside
    it, so the view does not change shape as you pan off the edge of the world.
    Without one, the frame is sized to the hexes you passed in.

    Returns (lines, origin). `origin` is the (column, row) the grid was shifted
    by, which is everything hex_at() needs to turn a mouse click back into a
    hex -- so the placement arithmetic lives in exactly one place.
    """
    cell = {}
    for t in tiles:
        cell[(t["q"], t["r"])] = paint(t["glyph"], t["colour"])
    for c in cities:
        cell[(c["q"], c["r"])] = paint("@", c["colour"], bold=True)
    for u in units:
        cell[(u["q"], u["r"])] = paint(u["glyph"], u["colour"], bold=True)
    for qr in highlight:
        if qr in cell:
            cell[qr] = paint(cell[qr].replace(RESET, ""), None, invert=True)

    if not cell and window is None:
        return ["(empty world -- try `n`)"], (0, 0)

    extent = None
    if cell:
        xs = [2 * q + r for q, r in cell]
        rs = [r for _, r in cell]
        extent = (min(xs), max(xs), min(rs), max(rs))

    left, right, low, high = window if window is not None else extent

    # One pass, dropping anything outside the frame.
    by_row = {}
    for (q, r), glyph in cell.items():
        x = 2 * q + r
        if left <= x <= right and low <= r <= high:
            by_row.setdefault(r, []).append((x, glyph))

    # Two kinds of caption, chosen by whether a window was asked for.
    #
    # Without one, the frame *is* the whole map, so the six points of the
    # hexagon are captioned in the empty margin around it. Note this cannot be
    # decided by looking at the hexes: they arrive pre-windowed by
    # map_window(), so they always appear to fill the frame exactly.
    if coords and window is None:
        for row, x, text, side in corner_labels(cell):
            start = x + 2 if side == "right" else x - 1 - len(text)
            if window is None:
                left = min(left, start)
                right = max(right, start + len(text) - 1)
            elif not (left <= start and start + len(text) - 1 <= right):
                continue                      # would not fit a fixed frame
            for i, char in enumerate(text):
                by_row.setdefault(row, []).append((start + i, paint(char, 244)))

    # One list per line, one entry per visible column. Kept unjoined until the
    # end: an entry is a whole escape-wrapped glyph, so once joined you can no
    # longer address a column by index.
    grid = []
    for row in range(low, high + 1):
        slots = [" "] * (right - left + 1)
        for x, glyph in by_row.get(row, ()):
            slots[x - left] = glyph
        grid.append(slots)
    grid.reverse()                            # +r now runs up the screen

    # With a window there is no margin and the world's corners are usually
    # off-screen, so caption the four corners of the screen itself with the
    # hex each one is showing. They sit on top of terrain, so they are dim,
    # and are skipped if two would collide.
    if coords and window is not None:
        width = right - left + 1
        taken = {}
        for line, column, text, side in frame_corners((left, high), width, len(grid)):
            start = column if side == "left" else column - len(text) + 1
            stop = start + len(text) - 1
            if start < 0 or stop >= width:
                continue
            if any(start <= e and s <= stop for s, e in taken.get(line, ())):
                continue
            taken.setdefault(line, []).append((start, stop))
            for i, char in enumerate(text):
                grid[line][start + i] = paint(char, 244)

    return ["".join(slots) for slots in grid], (left, high)



def frame_corners(origin, width, height):
    """Captions for the four corners of the frame itself.

    Windowed onto part of a large map there is no world corner in sight, so
    what is worth knowing is which hex each corner of the *screen* is showing.

    Columns alternate between hexes and the gaps between them, so a corner can
    land on a gap; step one column inwards when it does.
    """
    for line in {0, height - 1}:
        for column, side in ((0, "left"), (width - 1, "right")):
            spot = hex_at(origin, line, column)
            if spot is None:
                column += 1 if side == "left" else -1
                spot = hex_at(origin, line, column)
            if spot is not None:
                yield line, column, f"({spot[0]},{spot[1]})", side


def hex_at(origin, line, column):
    """Which hex is drawn at (line, column) of the map block, or None.

    The inverse of the placement in draw_map: a hex sits at column 2q + r and
    row r, both shifted by `origin`. Columns where 2q + r has the wrong parity
    fall between hexes and belong to nothing.
    """
    left, first_r = origin
    r = first_r - line                 # lines run bottom-up; see draw_map
    x = column + left
    return ((x - r) // 2, r) if (x - r) % 2 == 0 else None


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
    return "  " + "   ".join(
        f"{paint(str(u['unit_id']), u['colour'], bold=True)}:{u['type']}"
        f"({u['q']},{u['r']}) {u['moves_left']}mp" for u in units) if units else ""


def emit(lines):
    """Draw a frame from the top of the screen.

    Each line is erased to its right rather than clearing the whole screen
    first: at ten frames a second a full clear visibly flickers. The trailing
    erase-below removes anything left by a taller previous frame.
    """
    body = "\n".join(f"{line}\x1b[K" for line in lines)
    print(f"\x1b[H{body}\x1b[J", end="", flush=True)
