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


def draw_map(tiles, units, cities, highlight=frozenset()):
    """Return the map as a list of lines.

    Later layers win: terrain is overdrawn by cities, cities by units. A hex in
    `highlight` (a set of (q, r)) is inverted, which is how the client shows
    where the selected unit could walk.
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

    if not cell:
        return ["(empty world -- try `n`)"]

    left = min(2 * q + r for q, r in cell)
    width = max(2 * q + r for q, r in cell) - left + 1

    lines = []
    for row in range(min(r for _, r in cell), max(r for _, r in cell) + 1):
        slots = [" "] * width
        for (q, r), glyph in cell.items():
            if r == row:
                slots[2 * q + r - left] = glyph
        lines.append("".join(slots))
    return lines


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
