"""civ -- the windowed front-end.

The rules are in sql/, the statements in queries.py, and everything any
front-end needs in game.py. What is left here is what only a window needs:
turning rows into rectangles, and turning clicks back into tiles.

The one design decision worth stating: the SQL log is not a footer, it is a
column, as wide as the map is tall. The map is the input device; the log is the
artifact. If a statement went to the database, it is on screen at a size you
can read from the back of a lecture hall.

    python3 client/gui.py            two windows on one database are two players
"""

import sys
import time
from dataclasses import dataclass, field

import psycopg
import pygame

import game
import input_management as im     # for edit_line alone: one line editor, both
import palette
import render                      # for HEAT alone: one ramp, both front-ends
from db import DB

# --------------------------------------------------------------------- skin

INK = (233, 236, 241)
MUTED = (129, 138, 152)
FAINT = (78, 85, 97)
BACKDROP = (13, 15, 19)
PANEL = (21, 24, 30)
EDGE = (45, 50, 60)
ACCENT = (86, 156, 214)
ALARM = (226, 96, 88)
AMBER = (224, 168, 74)

# How far terrain is darkened before anything is drawn on top of it. The tile
# colours out of 03_seed.sql are chosen to be legible as one bright character
# on black; as a filled 40px square they are far too loud to read a number on.
TERRAIN_DIM = 0.55

# Three fixed steps rather than continuous zoom: a fractional tile size blurs
# the glyph and the coordinate ticks, and there is no third thing zoom is for.
TILE_STEPS = (32, 40, 56)

# Re-read the world this often, and immediately after anything you do. Five
# hertz is plenty to notice the other player, and the polling is itself the
# demo -- demo/02-scale.sql exists to show what this costs indexed and not.
POLL_SECONDS = 0.2

# Band heights at the reference size, scaled by height / 1000 thereafter.
HUD_H, CONTEXT_H, PROMPT_H = 72, 150, 78
RAIL_W, RAIL_H = 460, 310          # a column when wide, a band when narrow
NARROW = 1200
GUTTER = 24                        # coordinate ticks, left and below

DEFAULT_SIZE = (1600, 1000)        # fits a 1080p projector with room to spare

_FONTS = {}


def font(size, bold=False):
    """The bundled font, cached.

    Font(None) rather than SysFont("monospace"): this repository gets cloned
    onto machines whose idea of a monospace font is anyone's guess, and a
    missing font at the front of a lecture is not a recoverable situation.
    """
    if (size, bold) not in _FONTS:
        loaded = pygame.font.Font(None, size)
        loaded.set_bold(bold)
        _FONTS[(size, bold)] = loaded
    return _FONTS[(size, bold)]


def write(surface, string, at, size=20, colour=INK, bold=False, anchor="topleft"):
    """Draw one string and return where it landed, so callers can flow text."""
    image = font(size, bold).render(str(string), True, colour)
    return surface.blit(image, image.get_rect(**{anchor: at}))


# ------------------------------------------------------------------- layout
# Kept apart from drawing so that the geometry can be tested without a
# framebuffer, and so that "where is the map" has one answer.

@dataclass
class Bands:
    """The five regions of the window. An empty rect means "not shown"."""

    hud: pygame.Rect
    map: pygame.Rect
    context: pygame.Rect
    rail: pygame.Rect
    prompt: pygame.Rect
    ui: float                      # height / 1000, the one scale factor


def layout(size, focus="both"):
    """Carve the window up.

    Wide, the log is a column beside the map. Narrow -- a half-width window on
    a projector, which is how two players fit on one screen -- it becomes a
    full-width band below it: what matters in that lecture is the statement and
    the SQLSTATE, and a long statement reads better across the window than down
    a 460px column.
    """
    width, height = size
    ui = height / 1000.0
    hud_h, context_h, prompt_h = (round(band * ui)
                                  for band in (HUD_H, CONTEXT_H, PROMPT_H))

    hud = pygame.Rect(0, 0, width, hud_h)
    prompt = pygame.Rect(0, height - prompt_h, width, prompt_h)
    top, bottom = hud.bottom, prompt.top
    empty = pygame.Rect(0, 0, 0, 0)

    if focus == "map":
        # No log at all. The one time you want the whole window to be world.
        return Bands(hud, pygame.Rect(0, top, width, bottom - top - context_h),
                     pygame.Rect(0, bottom - context_h, width, context_h),
                     empty, prompt, ui)

    if focus == "log":
        # The inverse: the map keeps a thumbnail so you can still see what the
        # statements are talking about, and the log takes everything else.
        thumb = pygame.Rect(0, top, round(360 * ui), round(240 * ui))
        return Bands(hud, thumb, empty,
                     pygame.Rect(thumb.right, top, width - thumb.width,
                                 bottom - top),
                     prompt, ui)

    if width < NARROW:
        rail_h = round(RAIL_H * ui)
        rail = pygame.Rect(0, bottom - rail_h, width, rail_h)
        map_w, bottom = width, rail.top
    else:
        rail = pygame.Rect(width - RAIL_W, top, RAIL_W, bottom - top)
        map_w = rail.left

    context = pygame.Rect(0, bottom - context_h, map_w, context_h)
    return Bands(hud, pygame.Rect(0, top, map_w, context.top - top),
                 context, rail, prompt, ui)


@dataclass
class Board:
    """Which tiles are on screen, and where each one is.

    `window` is the same four numbers map_window() takes, so the question "what
    is in view" has one answer shared with the terminal. `area` is the tiles
    alone -- the coordinate gutter is outside it, which is what lets a click be
    tested against it directly.
    """

    area: pygame.Rect
    window: tuple
    tile_px: int

    def rect_of(self, x, y):
        """Where tile (x, y) lands on screen.

        Lines run bottom-up so that +y is north, which is the only thing
        tile_at() has to undo. The twin of render.tile_at()'s arithmetic in
        character cells; the duplication is deliberate, since what a tile *is*
        on screen is each front-end's own business and wrapping eight lines of
        this in a shared class would cost more attention than it saves.
        """
        x0, _, _, y1 = self.window
        return pygame.Rect(self.area.x + (x - x0) * self.tile_px,
                           self.area.y + (y1 - y) * self.tile_px,
                           self.tile_px, self.tile_px)

    def tile_at(self, pos):
        """Which tile is under a pixel, or None if the pixel missed the map."""
        if not self.area.collidepoint(pos):
            return None
        x0, _, _, y1 = self.window
        return (x0 + (pos[0] - self.area.x) // self.tile_px,
                y1 - (pos[1] - self.area.y) // self.tile_px)


def odd(n):
    """The largest odd number no greater than `n`, and at least 1.

    window_for() puts the same count of tiles either side of the centre, so the
    window it returns always spans an odd number of tiles. Asking it for an
    even count therefore gets you one more tile than you asked for -- which,
    before this, was fetched and then drawn off the bottom of the board.
    An odd board also has a true centre tile, which is what `v` means.
    """
    return max(1, n - 1 + n % 2)


def board_for(band, centre, tile_px):
    """Fit whole tiles into a map band, centred on `centre`.

    Rounded down to whole tiles and then centred in the band: half a tile at
    the edge looks like a rendering bug, and the leftover pixels are less
    noticeable split between the two sides.
    """
    wide = odd((band.width - GUTTER) // tile_px)
    high = odd((band.height - GUTTER) // tile_px)
    area = pygame.Rect(0, 0, wide * tile_px, high * tile_px)
    area.topleft = (band.left + GUTTER + (band.width - GUTTER - area.width) // 2,
                    band.top + (band.height - GUTTER - area.height) // 2)
    return Board(area, game.window_for(centre, wide, high), tile_px)


# -------------------------------------------------------------------- state
# Session is what any front-end holds. This is what only a window does: how big
# the tiles are, which pixel the mouse is over, whether an Esc is armed.

@dataclass
class View:
    tile_px: int = TILE_STEPS[1]
    focus: str = "both"                        # 'both' | 'map' | 'log'
    hover: tuple | None = None                 # tile under the cursor
    panning: bool = False
    drag: tuple = (0, 0)                       # unspent drag, in pixels
    escape_armed: bool = False
    last_click: tuple = (0.0, None)            # (when, which tile), for
                                               # double-click-to-centre
    selected: int | None = None                # a unit id, like `m 3 ` typed
    trouble: str = ""                          # the last refusal, shown as-is
    ants: int = 0                              # marching-ants phase, in pixels
    tray: str | None = None                    # which list is open, if any
    placing: str | None = None                 # a bought unit, awaiting a tile
    line: dict = field(default_factory=lambda: {"text": "", "at": None,
                                                "draft": ""})
    history: list = field(default_factory=list)   # typed lines only, not clicks
    result: list | None = None                 # rows a typed statement returned
    newgame: dict = field(default_factory=      # what the New game tray holds
                          lambda: {"width": 40, "height": 24,
                                   "civs": 2, "seed": 42})

    def zoom(self, by):
        step = TILE_STEPS.index(self.tile_px) + by
        self.tile_px = TILE_STEPS[max(0, min(len(TILE_STEPS) - 1, step))]


def read_world(db, session, view, size):
    """One poll: everything a frame is drawn from.

    Six statements, none of them echoed -- the log would drown in redraws,
    which is the split db.py already makes. The bands and the board are kept
    with the rows they were fetched for, so a resize cannot draw tiles into a
    grid they were not read for.
    """
    bands = layout(size, view.focus)
    snap = game.snapshot(db, session)
    if snap is None:
        return {"bands": bands, "snap": None}

    # Small map bands get the smallest tiles: a half-width window is how two
    # players fit on one projector, and a thumbnail is meant to show the shape
    # of the world rather than be played on.
    tile_px = view.tile_px
    if size[0] < NARROW or view.focus == "log":
        tile_px = min(tile_px, TILE_STEPS[0])
    board = board_for(bands.map, game.view_centre(db, session), tile_px)
    cells, highest = game.tiles_in(db, session, board.window)
    world = {"bands": bands, "snap": snap, "board": board,
             "cells": cells, "highest": highest}

    # A tray's rows are read while it is open and not otherwise: three more
    # statements per frame to keep a closed menu up to date would be three
    # statements a second spent on nothing. A unit being placed still needs
    # the shop, for its price.
    if view.tray == "buy" or view.placing:
        world["shop"] = game.unit_shop(db, snap["civ_id"])
    if view.tray == "research":
        world["techs"] = game.available_techs(db, snap["civ_id"])
    if view.tray == "civs":
        world["civs"] = game.all_civs(db)
    return world


def tile_colours(cells, highest):
    """(x, y) -> the RGB each tile is filled with.

    Two shapes arrive here. Terrain carries a `colour`, an ANSI number out of
    03_seed.sql, and is darkened so that what is drawn on top of it can be
    read. An overlay carries a `value`, and goes through the same HEAT ramp the
    terminal uses, undimmed -- the colour *is* the information there.
    """
    if highest is None:
        return {(cell["x"], cell["y"]):
                palette.dim(palette.rgb(cell["colour"]), TERRAIN_DIM)
                for cell in cells}
    return {(cell["x"], cell["y"]):
            palette.rgb(render.heat(cell["value"], highest)) for cell in cells}


# ------------------------------------------------------------------ drawing

def draw_board(surface, board, world, session, view):
    """Terrain, then what is standing on it. Later layers win, as in the
    terminal: terrain is overdrawn by cities, cities by units."""
    fill = tile_colours(world["cells"], world["highest"])
    px = board.tile_px

    for spot, colour in fill.items():
        surface.fill(colour, board.rect_of(*spot))

    # The edge of the world. Tiles run 0..width-1, and the frame is kept the
    # size of the viewport even when you pan off the map, so without this the
    # emptiness beyond the last tile looks like a drawing fault rather than
    # the end of the world.
    edge = board.rect_of(0, world["snap"]["world"]["height"] - 1)
    edge.width = world["snap"]["world"]["width"] * px
    edge.height = world["snap"]["world"]["height"] * px
    if board.area.colliderect(edge):
        pygame.draw.rect(surface, EDGE, edge.clip(board.area), 1)

    # The overlay's numbers, drawn big. A heat map you can only read as colour
    # is a decoration; with the value on it you can also read it as data.
    if world["highest"] is not None:
        for cell in world["cells"]:
            rect = board.rect_of(cell["x"], cell["y"])
            write(surface, cell["value"], rect.center, size=round(px * 0.6),
                  colour=palette.readable_on(fill[(cell["x"], cell["y"])]),
                  bold=True, anchor="center")

    # Where the selected unit could walk, and what each tile would cost it.
    # A wash rather than an outline per tile, so a sixteen-tile reachable set
    # reads as one shape; the number in the corner is the recursive CTE's own
    # output, which both front-ends used to fetch and throw away.
    for spot, cost in session.highlight.items():
        rect = board.rect_of(*spot)
        if not board.area.contains(rect):
            continue
        wash = pygame.Surface(rect.size, pygame.SRCALPHA)
        wash.fill((*INK, 64))
        surface.blit(wash, rect)
        pygame.draw.rect(surface, INK, rect.inflate(-4, -4), 1)
        if cost:
            write(surface, cost, (rect.left + 3, rect.top + 2),
                  size=round(px * 0.34), colour=INK)

    for city in world["snap"]["cities"]:
        rect = board.rect_of(city["x"], city["y"]).inflate(-px // 6, -px // 6)
        colour = palette.rgb(city["colour"])
        pygame.draw.rect(surface, colour, rect, border_radius=px // 6)
        pygame.draw.rect(surface, INK, rect, 2, border_radius=px // 6)
        write(surface, city["population"], rect.center, size=round(px * 0.55),
              colour=palette.readable_on(colour), bold=True, anchor="center")
        # The name sits on top of the map, and often on top of whatever is
        # standing on the next tile down, so it gets its own backing.
        size = round(px * 0.4)
        pill = font(size).size(city["name"])
        plate = pygame.Rect(0, 0, pill[0] + 6, pill[1])
        plate.midtop = (rect.centerx, rect.bottom + 3)
        pygame.draw.rect(surface, BACKDROP, plate, border_radius=3)
        write(surface, city["name"], plate.center, size=size, colour=INK,
              anchor="center")

    for unit in world["snap"]["units"]:
        rect = board.rect_of(unit["x"], unit["y"])
        colour = palette.rgb(unit["colour"])
        # Both budgets spent is the most useful thing to see at a glance in a
        # turn-based game, and UNITS already fetches them.
        spent = unit["moves_left"] == 0 and unit["actions_left"] == 0
        if spent:
            colour = palette.dim(colour, 0.45)
        pygame.draw.circle(surface, colour, rect.center, px * 0.36)
        pygame.draw.circle(surface, FAINT if spent else INK, rect.center,
                           px * 0.36, 2)
        write(surface, unit["glyph"], rect.center, size=round(px * 0.5),
              colour=palette.readable_on(colour), bold=True, anchor="center")

        # hp as a bar under the circle, against unit_type.max_hp: 4hp of 20 and
        # 4 of 10 are different decisions, and a bare number makes you do the
        # division yourself.
        full = round(px * 0.6)
        bar = pygame.Rect(0, 0, full, max(3, px // 12))
        bar.midtop = (rect.centerx, rect.centery + round(px * 0.36) + 2)
        surface.fill(EDGE, bar)
        left = full * unit["hp"] // unit["max_hp"]
        surface.fill(colour, (bar.x, bar.y, left, bar.height))

        if unit["unit_id"] == view.selected:
            # Marching ants, and the only animation in the window: motion
            # locates the selection across a lecture hall faster than colour.
            dashed_rect(surface, INK, rect.inflate(-2, -2), view.ants)
        elif (view.hover == (unit["x"], unit["y"])
              and view.selected is not None
              and unit["civ_id"] != world["snap"]["civ_id"]):
            # What a click here would do. attack() decides whether it is
            # allowed; this only says which statement is about to go out.
            pygame.draw.circle(surface, ALARM, rect.center, px * 0.44, 3)

    if view.hover is not None:
        hovered = board.rect_of(*view.hover)
        if board.area.contains(hovered):
            pygame.draw.rect(surface, INK, hovered, 2)


def dashed_rect(surface, colour, rect, phase, dash=7, width=3):
    """A ring of marching ants around `rect`, offset by `phase` pixels.

    The perimeter is walked as a list of points so that the dashes run
    continuously round the corners; drawing four dashed lines instead makes
    them stutter where the edges meet.
    """
    edge = ([(x, rect.top) for x in range(rect.left, rect.right)]
            + [(rect.right - 1, y) for y in range(rect.top, rect.bottom)]
            + [(x, rect.bottom - 1) for x in range(rect.right - 1, rect.left, -1)]
            + [(rect.left, y) for y in range(rect.bottom - 1, rect.top, -1)])
    for step in range(0, len(edge), 2):
        if ((step + phase) // dash) % 2 == 0:
            x, y = edge[step]
            surface.fill(colour, (x - width // 2, y - width // 2, width, width))


def draw_gutter(surface, board, extent, ui):
    """Coordinates in a margin, ticked every five tiles.

    This replaces the terminal's corner captions, which had to be drawn on top
    of terrain and dropped when two of them collided. A gutter has room, so the
    collision logic is gone rather than ported.
    """
    x0, x1, y0, y1 = board.window
    width, height = extent
    size = max(11, round(15 * ui))
    # Only tiles the world actually has. Off the edge of the map the numbers
    # are still meaningful, but labelling them crowds the corner with
    # coordinates of places that do not exist.
    for x in range(max(x0, 0) - max(x0, 0) % 5, min(x1, width - 1) + 1, 5):
        rect = board.rect_of(x, y0)
        write(surface, x, (rect.centerx, board.area.bottom + 4), size=size,
              colour=MUTED, anchor="midtop")
    for y in range(max(y0, 0) - max(y0, 0) % 5, min(y1, height - 1) + 1, 5):
        rect = board.rect_of(x0, y)
        write(surface, y, (board.area.left - 5, rect.centery), size=size,
              colour=MUTED, anchor="midright")


def draw_hud(surface, rect, snap, session, view, ui):
    """Turn, whose window this is, the purse, and what is being researched."""
    if not rect.height:
        return
    surface.fill(PANEL, rect)
    pygame.draw.line(surface, EDGE, rect.bottomleft, rect.bottomright)

    civ = snap["civ"]
    pad = round(18 * ui)
    big, small = round(30 * ui), round(21 * ui)
    middle = rect.top + rect.height // 4
    at = write(surface, f"turn {snap['world']['turn']}",
               (rect.left + pad, middle), size=big, bold=True,
               anchor="midleft")

    # The civ chip is filled with civ.colour, so in a two-window demo you can
    # tell at a glance which window is Rome without reading anything.
    colour = palette.rgb(civ["colour"])
    chip = pygame.Rect(at.right + pad, 0, round(150 * ui), round(28 * ui))
    chip.centery = middle
    pygame.draw.rect(surface, colour, chip, border_radius=round(6 * ui))
    write(surface, civ["name"], chip.center, size=small,
          colour=palette.readable_on(colour), bold=True, anchor="center")

    at = write(surface, f"{civ['gold']}g   {civ['science']} science",
               (chip.right + pad, middle), size=small, anchor="midleft")

    researching = civ["researching"]
    write(surface, f"researching {researching}" if researching
          else "researching nothing", (at.right + pad, middle), size=small,
          colour=INK if researching else FAINT, anchor="midleft")

    # The buttons, and the segmented overlay control on the right. `on` for
    # the overlay comes from session.overlay, so the control reports the state
    # rather than remembering its own.
    showing = session.overlay or "terrain"
    for key, slot in hud_slots(rect, ui).items():
        if key.startswith("overlay:"):
            which = key.removeprefix("overlay:")
            label = dict(OVERLAY_TABS)[which]
            button(surface, slot, label, ui, on=which == showing)
        else:
            button(surface, slot, dict(HUD_ACTIONS)[key], ui,
                   on=view.tray == key)


def draw_context(surface, rect, snap, view, world, legend, ui):
    """What the cursor is over, and what your units have left to spend."""
    if not rect.height:
        return
    surface.fill(PANEL, rect)
    pygame.draw.line(surface, EDGE, rect.topleft, rect.topright)

    pad = round(16 * ui)
    size = round(22 * ui)
    line = rect.top + pad

    # Whatever the database last refused, in its own words. Showing it is the
    # point: unit_one_per_tile rejecting a move is a lecture beat, not an
    # error to be smoothed over.
    if view.trouble:
        for chunk in wrap(view.trouble, rect.width - 2 * pad, size)[:2]:
            write(surface, chunk, (rect.left + pad, line), size=size,
                  colour=ALARM)
            line += size

    # The hover readout is free: the terrain for every tile in view was already
    # fetched to draw it, so reading it back costs no statement.
    if view.hover is not None:
        under = {(cell["x"], cell["y"]): cell for cell in world["cells"]}
        cell = under.get(view.hover, {})
        label = f"({view.hover[0]},{view.hover[1]})"
        kind = legend.get(cell.get("glyph"))
        if kind:
            label += (f"  {kind['code']}   "
                      f"F{kind['food']} P{kind['production']} G{kind['gold']}")
        elif "value" in cell:                  # an overlay is showing instead
            label += f"  {world['snap']['civ']['name']} would gather {cell['value']}"
        write(surface, label, (rect.left + pad, line), size=size, colour=MUTED)
    line += size

    units = snap["my_units"]

    for key, slot in context_slots(rect, snap, view, ui).items():
        if key == "end_turn":
            # The largest button on screen, and amber once everything you own
            # has spent both its budgets -- the moment there is nothing left to
            # do but pass.
            done = all(u["moves_left"] == 0 and u["actions_left"] == 0
                       for u in units)
            pygame.draw.rect(surface, AMBER if done else PANEL, slot,
                             border_radius=round(6 * ui))
            pygame.draw.rect(surface, AMBER, slot, 2,
                             border_radius=round(6 * ui))
            write(surface, "END TURN", slot.center, size=round(24 * ui),
                  colour=BACKDROP if done else AMBER, bold=True,
                  anchor="center")
        else:
            button(surface, slot, "Found city", ui)

    if not units:
        write(surface, "no units", (rect.left + pad, line), size=size,
              colour=FAINT)
        return

    at = rect.left + pad
    for unit in units:
        colour = palette.rgb(unit["colour"])
        spent = unit["moves_left"] == 0 and unit["actions_left"] == 0
        chunk = (f"{unit['unit_id']}:{unit['type']}"
                 f"({unit['x']},{unit['y']}) {unit['hp']}hp "
                 f"{unit['moves_left']}mp {unit['actions_left']}ap")
        drawn = write(surface, chunk, (at, line), size=size,
                      colour=FAINT if spent else colour)
        at = drawn.right + round(24 * ui)
        if at > rect.right - round(260 * ui):        # wrap rather than run off
            at, line = rect.left + pad, line + size
            if line > rect.bottom - size:
                break


def draw_rail(surface, rect, log, ui):
    """Every statement that changed the world, newest first.

    Three brightnesses rather than one: from the back of a room you can always
    read the statement that just went out, and the ones before it are context
    you can lean in for.
    """
    if not rect.height:
        return
    surface.fill(PANEL, rect)
    pygame.draw.line(surface, EDGE, rect.topleft, rect.bottomleft)

    pad = round(16 * ui)
    write(surface, "SQL", (rect.left + pad, rect.top + pad),
          size=round(20 * ui), colour=MUTED, bold=True)

    line = rect.top + pad + round(30 * ui)
    for age, statement in enumerate(reversed(log)):
        size, colour = ((round(22 * ui), INK) if age == 0 else
                        (round(18 * ui), MUTED) if age < 4 else
                        (round(15 * ui), FAINT))
        if age == 0:
            pygame.draw.rect(surface, ACCENT,
                             (rect.left, line - 4, round(3 * ui), size + 8))
        for chunk in wrap(statement, rect.width - 2 * pad, size):
            if line > rect.bottom - size:
                return
            write(surface, chunk, (rect.left + pad, line), size=size,
                  colour=colour)
            line += size
        line += round(8 * ui)


def wrap(statement, width, size):
    """Break a statement to fit the rail. Words, then characters if a single
    word is wider than the column -- which a long mogrified literal can be."""
    measure = font(size).size
    lines, current = [], ""
    for word in str(statement).split():
        candidate = f"{current} {word}".strip()
        if measure(candidate)[0] <= width or not current:
            current = candidate
        else:
            lines.append(current)
            current = word
    return lines + [current] if current else lines


KEYS_HELP = ("click a unit then a square to move   arrows step it   "
             "shift+B buy   shift+T research   shift+C found   "
             "space ends the turn   drag pans   Home follows   +/- zoom   "
             "type SQL or a command and press enter   Esc Esc quits")


def reading(text):
    """How a typed line will be taken: "command", "SQL", or None if empty.

    One letter, alone or followed by a space, is a command -- the same letters
    the terminal uses, so notes and muscle memory survive. Anything else is
    SQL. Shown live beside the prompt, because guessing wrong about which of
    the two you are typing is the only way this bar can surprise you.
    """
    stripped = text.strip()
    if not stripped:
        return None
    if stripped[0] in TYPED and (len(stripped) == 1 or stripped[1] == " "):
        return "command"
    return "SQL"


def draw_prompt(surface, rect, view, db, ui):
    """The prompt, its sigil, and what it thinks you are typing.

    The sigil is the transaction state, straight off the connection: `>` when
    autocommit is doing its usual thing, an amber `BEGIN>` once a typed BEGIN
    has opened a transaction. Lecture 21 is about the second one.
    """
    if not rect.height:
        return
    surface.fill(PANEL, rect)
    pygame.draw.line(surface, EDGE, rect.topleft, rect.topright)

    pad = round(16 * ui)
    size = round(21 * ui)
    inside = db is not None and db.in_transaction()
    sigil = "BEGIN>" if inside else ">"
    at = write(surface, sigil, (rect.left + pad, rect.centery), size=size,
               colour=AMBER if inside else MUTED, bold=inside, anchor="midleft")

    if not view.line["text"]:
        write(surface, KEYS_HELP, (at.right + pad, rect.centery),
              size=round(18 * ui), colour=FAINT, anchor="midleft")
        return

    # Shift+Enter puts a newline in, for BEGIN; ... COMMIT; as one submission.
    # Only the last two lines are shown; the bar is one band tall, not a pane.
    lines = view.line["text"].split("\n")[-2:]
    for index, text in enumerate(lines):
        write(surface, text + ("_" if index == len(lines) - 1 else ""),
              (at.right + pad, rect.centery
               + round((index - (len(lines) - 1) / 2) * size)),
              size=size, anchor="midleft")

    tag = reading(view.line["text"])
    write(surface, tag, (rect.right - pad, rect.centery), size=round(18 * ui),
          colour=ACCENT if tag == "SQL" else MUTED, bold=True, anchor="midright")


def draw_result(surface, bands, view):
    """The rows a typed statement returned, over the map.

    Columns are measured rather than padded, because the window's font is not
    monospaced -- the same job render.format_rows does with str.ljust, done in
    pixels. EXPLAIN comes back as one text column and is left exactly as
    PostgreSQL indented it: the plan tree is the artifact.
    """
    if view.result is None:
        return {}
    ui = bands.ui
    pad = round(14 * ui)
    size = round(19 * ui)
    rows = view.result

    # As tall as it needs to be and no taller. A two-row answer in a panel
    # sized for twenty reads as an error.
    chrome = round(52 * ui) + (0 if not rows else 2 * size)
    wanted = chrome + size * max(1, len(rows))
    panel = pygame.Rect(0, 0, bands.map.width - round(48 * ui),
                        min(wanted, bands.map.height - round(32 * ui)))
    panel.midbottom = (bands.map.centerx, bands.map.bottom - round(16 * ui))
    pygame.draw.rect(surface, PANEL, panel, border_radius=round(8 * ui))
    pygame.draw.rect(surface, ACCENT, panel, 1, border_radius=round(8 * ui))
    close = pygame.Rect(panel.right - round(34 * ui), panel.top + round(8 * ui),
                        round(26 * ui), round(26 * ui))
    write(surface, "Esc", close.center, size=round(17 * ui), colour=FAINT,
          anchor="center")

    if not rows:
        write(surface, "(no rows)", (panel.left + pad, panel.top + pad),
              size=size, colour=MUTED)
        return {"result_close": close}

    columns = list(rows[0])
    plan = columns == ["QUERY PLAN"]
    line = panel.top + pad
    step = size

    if not plan:
        widths = [max(font(size, True).size(name)[0],
                      *(font(size).size(str(row[name]))[0] for row in rows[:60]))
                  + round(18 * ui) for name in columns]
        at = panel.left + pad
        for name, width in zip(columns, widths):
            write(surface, name, (at, line), size=size, colour=MUTED, bold=True)
            at += width
        line += step
        pygame.draw.line(surface, EDGE, (panel.left + pad, line),
                         (panel.right - pad, line))
        line += round(6 * ui)

    shown = 0
    for row in rows:
        if line + step > panel.bottom - round(32 * ui):
            break
        if plan:
            # No ljust and no strip: the indentation is the tree.
            write(surface, row["QUERY PLAN"], (panel.left + pad, line),
                  size=size)
        else:
            at = panel.left + pad
            for name, width in zip(columns, widths):
                write(surface, row[name], (at, line), size=size)
                at += width
        line, shown = line + step, shown + 1

    write(surface, f"{shown} of {len(rows)} row{'' if len(rows) == 1 else 's'}",
          (panel.left + pad, panel.bottom - round(20 * ui)), size=round(17 * ui),
          colour=MUTED, anchor="midleft")
    return {"result_close": close}


# ------------------------------------------------------------------- chrome
# Buttons and trays. Every clickable thing is a rect produced by one of the
# *_slots functions below, and both drawing and hit-testing read the same
# function -- so a button cannot end up somewhere other than where it works.
#
# What a button is *enabled* by always comes from something the database
# publishes: unit_shop.unlocked, unit_type.founds_cities, available_tech. Where
# there is no such view, the button stays enabled and the refusal does the
# explaining.

HUD_ACTIONS = (("new", "New game"), ("buy", "Buy"), ("research", "Research"),
               ("civs", "Play as"), ("help", "?"))

# The segmented overlay control. "terrain" is the absence of an overlay, and
# the values are the ones game.OVERLAYS maps onto.
OVERLAY_TABS = (("terrain", "Terrain"), ("food", "Food"),
                ("production", "Prod"), ("gold", "Gold"))

NEW_GAME_FIELDS = (("width", 4, 400), ("height", 4, 400),
                   ("civs", 1, 12), ("seed", 0, 9999))


def button(surface, rect, label, ui, *, on=False, enabled=True):
    """One button. Filled when it is the current choice, outlined otherwise,
    and greyed when the database says it is not available."""
    body = ACCENT if on else PANEL
    ink = BACKDROP if on else (INK if enabled else FAINT)
    pygame.draw.rect(surface, body, rect, border_radius=round(5 * ui))
    pygame.draw.rect(surface, ACCENT if on else EDGE, rect, 1,
                     border_radius=round(5 * ui))
    write(surface, label, rect.center, size=round(20 * ui), colour=ink,
          bold=on, anchor="center")


def flow(labels, left, top, height, ui, gap=None):
    """Lay a row of buttons out left to right, each as wide as its label.

    Returns [(key, rect)] in order, so a caller can draw them and a click can
    be tested against the same rects.
    """
    gap = round(8 * ui) if gap is None else gap
    size = round(20 * ui)
    at, out = left, []
    for key, label in labels:
        width = font(size).size(label)[0] + round(22 * ui)
        out.append((key, pygame.Rect(at, top, width, height)))
        at += width + gap
    return out


def hud_slots(rect, ui):
    """The HUD's clickable things: name -> rect.

    The status line gets the top half and the buttons the bottom, which is why
    the band is two rows deep. Squeezing both onto one line works at 1600px and
    then collides the moment the window is halved for a two-player demo.
    """
    if not rect.height:
        return {}
    height = round(30 * ui)
    top = rect.top + rect.height // 2 + (rect.height // 2 - height) // 2
    slots = dict(flow(HUD_ACTIONS, rect.left + round(18 * ui), top, height, ui))

    # The overlay tabs are pinned to the right, as one segmented control.
    tabs = flow(OVERLAY_TABS, 0, top, height, ui, gap=0)
    span = tabs[-1][1].right - tabs[0][1].left
    shift = rect.right - round(18 * ui) - span
    slots.update({f"overlay:{key}": tab.move(shift, 0) for key, tab in tabs})
    return slots


def context_slots(rect, snap, view, ui):
    """The context strip's buttons.

    `Found city` appears only for a unit that can actually found one, which is
    unit_type.founds_cities and an action still in hand -- both already on the
    row the map was drawn from. It is not a rule the client knows; it is a
    column it can read.
    """
    if not rect.height:
        return {}
    height = round(44 * ui)
    slots = {}

    end = pygame.Rect(0, 0, round(200 * ui), height)
    end.bottomright = (rect.right - round(18 * ui), rect.bottom - round(14 * ui))
    slots["end_turn"] = end

    picked = next((u for u in snap["my_units"]
                   if u["unit_id"] == view.selected), None)
    if picked and picked["founds_cities"] and picked["actions_left"] >= 1:
        found = pygame.Rect(0, 0, round(150 * ui), height)
        found.bottomleft = (rect.left + round(18 * ui),
                            rect.bottom - round(14 * ui))
        slots["found"] = found
    return slots


TRAY_ROW_H = 38


def tray_rect(bands, rows=()):
    """Where a tray sits: over the map, anchored to its bottom-left, and only
    as tall as what is in it.

    Over the map rather than over the log, because the log is the artifact
    being taught and covering it up to show a menu would be hiding the wrong
    thing.
    """
    ui = bands.ui
    high = round((64 + TRAY_ROW_H * max(1, len(rows))) * ui)
    rect = pygame.Rect(0, 0, round(420 * ui),
                       min(high, bands.map.height - round(32 * ui)))
    rect.bottomleft = (bands.map.left + round(24 * ui),
                       bands.map.bottom - round(16 * ui))
    return rect


def tray_slots(rect, rows, ui):
    """One rect per row of a tray, under its title."""
    height = round(TRAY_ROW_H * ui)
    top = rect.top + round(52 * ui)
    out = []
    for index, (key, *_rest) in enumerate(rows):
        line = pygame.Rect(rect.left + round(12 * ui), top + index * height,
                           rect.width - round(24 * ui), height - round(4 * ui))
        if line.bottom > rect.bottom - round(12 * ui):
            break
        out.append((key, line))
    return out


def tray_contents(view, world):
    """(title, rows) for the open tray, where a row is
    (key, label, right, note, enabled, colour).

    The rows are the database's own rows, reshaped. Nothing is filtered out --
    a locked unit is shown greyed with what it needs, so the shop doubles as a
    reason to research something.
    """
    if view.tray == "buy":
        rows = [(f"buy:{u['code']}", u["code"], f"{u['cost']}g",
                 f"{u['moves']}mp  str {u['strength']}" if u["unlocked"]
                 else f"needs {u['required_tech']}", u["unlocked"], None)
                for u in world.get("shop", ())]
        return "Buy a unit", rows
    if view.tray == "research":
        rows = [(f"tech:{t['code']}", t["code"], str(t["cost"]), "", True, None)
                for t in world.get("techs", ())]
        return "Research", rows
    if view.tray == "civs":
        rows = [(f"civ:{c['civ_id']}", c["name"], f"#{c['civ_id']}", "", True,
                 palette.rgb(c["colour"])) for c in world.get("civs", ())]
        return "Play as", rows
    if view.tray == "new":
        rows = [(f"field:{name}", name, str(view.newgame[name]), "", True, None)
                for name, _low, _high in NEW_GAME_FIELDS]
        return "New game", rows + [("deal", "Deal a world", "", "", True, None)]
    if view.tray == "help":
        return "Keys", [(f"help:{n}", line, "", "", False, None)
                        for n, line in enumerate(KEYS_HELP.split("   "))]
    return None, []


def draw_tray(surface, bands, view, world):
    """The open tray, if any, and the ghost of a unit waiting to be placed."""
    if view.tray is None:
        return {}
    ui = bands.ui
    title, rows = tray_contents(view, world)
    rect = tray_rect(bands, rows)
    pygame.draw.rect(surface, PANEL, rect, border_radius=round(8 * ui))
    pygame.draw.rect(surface, EDGE, rect, 1, border_radius=round(8 * ui))
    write(surface, title, (rect.left + round(16 * ui), rect.top + round(16 * ui)),
          size=round(24 * ui), bold=True)
    write(surface, "Esc", (rect.right - round(16 * ui), rect.top + round(18 * ui)),
          size=round(18 * ui), colour=FAINT, anchor="topright")

    slots = dict(tray_slots(rect, rows, ui))
    for key, label, right, note, enabled, colour in rows:
        line = slots.get(key)
        if line is None:
            continue
        ink = INK if enabled else FAINT
        if key.startswith("field:") or key == "deal":
            # A spinner, not a row: the value sits between two steppers.
            button(surface, line, "", ui)
            write(surface, label, (line.left + round(12 * ui), line.centery),
                  size=round(20 * ui), colour=ink, anchor="midleft")
            if key == "deal":
                continue
            write(surface, right, (line.centerx + round(40 * ui), line.centery),
                  size=round(20 * ui), colour=INK, bold=True, anchor="center")
            for sign, edge in (("-", line.centerx), ("+", line.right - round(30 * ui))):
                write(surface, sign, (edge + round(15 * ui), line.centery),
                      size=round(24 * ui), colour=ACCENT, anchor="center")
            continue

        if enabled:
            pygame.draw.rect(surface, BACKDROP, line, border_radius=round(4 * ui))
        if colour:
            chip = pygame.Rect(line.left + round(8 * ui), 0, round(14 * ui),
                               round(14 * ui))
            chip.centery = line.centery
            pygame.draw.rect(surface, colour, chip, border_radius=2)
        write(surface, label, (line.left + round(30 * ui if colour else 12 * ui),
                               line.centery), size=round(21 * ui), colour=ink,
              anchor="midleft")
        if right:
            write(surface, right, (line.right - round(12 * ui), line.centery),
                  size=round(21 * ui), colour=ink, bold=True, anchor="midright")
        if note:
            write(surface, note, (line.right - round(12 * ui), line.centery),
                  size=round(17 * ui), colour=FAINT, anchor="midright") \
                if not right else write(
                    surface, note, (line.centerx, line.centery),
                    size=round(17 * ui), colour=FAINT, anchor="center")
    return slots


def draw_ghost(surface, board, view, world):
    """The unit a buy tray handed to the cursor, following it around.

    Illegal tiles are not greyed out. buy_unit knows where a unit may stand and
    says so in a sentence; a client that greyed them would be repeating a rule.
    """
    if not view.placing or view.hover is None:
        return
    rect = board.rect_of(*view.hover)
    if not board.area.contains(rect):
        return
    px = board.tile_px
    ghost = pygame.Surface(rect.size, pygame.SRCALPHA)
    kind = next((u for u in world.get("shop", ())
                 if u["code"] == view.placing), None)
    pygame.draw.circle(ghost, (*INK, 110), (px // 2, px // 2), px * 0.36)
    surface.blit(ghost, rect)
    write(surface, view.placing[0], rect.center, size=round(px * 0.5),
          colour=BACKDROP, bold=True, anchor="center")
    if kind:
        write(surface, f"{kind['cost']}g", (rect.centerx, rect.top - 2),
              size=round(px * 0.34), colour=INK, anchor="midbottom")


def draw(surface, world, session, view, legend=None, db=None):
    """One frame, from the rows the last poll fetched."""
    bands = world["bands"]
    legend = legend or {}
    surface.fill(BACKDROP)

    if world["snap"] is None:
        write(surface, "No world yet.", surface.get_rect().center,
              size=round(40 * bands.ui), bold=True, anchor="midbottom")
        write(surface, "Press shift+N to deal one, or type `n 40 24 2 42`.",
              surface.get_rect().center, size=round(22 * bands.ui),
              colour=MUTED, anchor="midtop")
        # The prompt and the New game tray still work: both are the way out of
        # this state, so neither can be behind the world existing.
        draw_prompt(surface, bands.prompt, view, db, bands.ui)
        draw_tray(surface, bands, view, world)
        return

    draw_board(surface, world["board"], world, session, view)
    draw_gutter(surface, world["board"],
                (world["snap"]["world"]["width"],
                 world["snap"]["world"]["height"]), bands.ui)
    draw_hud(surface, bands.hud, world["snap"], session, view, bands.ui)
    draw_context(surface, bands.context, world["snap"], view, world, legend,
                 bands.ui)
    draw_rail(surface, bands.rail, session.log, bands.ui)
    draw_prompt(surface, bands.prompt, view, db, bands.ui)
    draw_ghost(surface, world["board"], view, world)
    draw_result(surface, bands, view)
    draw_tray(surface, bands, view, world)


# --------------------------------------------------------------------- input
# Returns True to keep the loop going, and True from `stale` to force a re-read
# rather than waiting out the poll -- latency is only felt on your own actions.
#
# Nothing here checks whether an action is legal. A target on the map is a
# click, the statement goes out, and if the database refuses it the refusal is
# what appears on screen. The one exception is which statement to send: an
# enemy on the tile means attack(), an empty tile means move_unit(), and that
# is the same choice the terminal player makes by typing `a` or `m`.

PAN_KEYS = {pygame.K_LEFT: (-1, 0), pygame.K_RIGHT: (1, 0),
            pygame.K_DOWN: (0, -1), pygame.K_UP: (0, 1)}

ZOOM_KEYS = {pygame.K_PLUS: 1, pygame.K_EQUALS: 1, pygame.K_KP_PLUS: 1,
             pygame.K_MINUS: -1, pygame.K_KP_MINUS: -1}

DOUBLE_CLICK = 0.35


def handle(event, db, session, view, world):
    """Deal with one event. Returns (keep_running, stale)."""
    if event.type == pygame.QUIT:
        return False, False

    if event.type in (pygame.VIDEORESIZE, pygame.WINDOWSIZECHANGED):
        # The board is fetched for a particular window of tiles, so a resize
        # has to re-read rather than stretch what it already has.
        return True, True

    if event.type == pygame.KEYDOWN:
        return key_down(event, db, session, view, world)

    board = world.get("board")

    if event.type == pygame.MOUSEMOTION:
        if board is not None:
            view.hover = board.tile_at(event.pos)
        if view.panning:
            return True, drag(db, session, view, event.rel)
        return True, False

    if event.type == pygame.MOUSEBUTTONDOWN:
        if event.button in (2, 3):
            view.panning, view.drag = True, (0, 0)
        elif event.button == 1 and board is not None:
            handled = click_chrome(db, session, view, world, event.pos)
            if handled is not None:
                return True, handled
            if maybe_centre(db, session, view, board, event.pos):
                return True, True
            return True, click(db, session, view, world, event.pos)

    if event.type == pygame.MOUSEBUTTONUP and event.button in (2, 3):
        view.panning = False

    return True, False


# The terminal's one-letter commands, so that lecture notes and muscle memory
# survive the move to a window. Deliberately not shared with civ.py's COMMANDS:
# a shared table would have to hand back front-end-neutral results, and the
# terminal would lose its greyed shop rows and its colour-coded civ names to
# get there. Parsing is the cheap half to duplicate.

def typed_overlay(session, args):
    session.overlay = game.OVERLAYS.get(args[0].lower()) if args else None


TYPED = {
    "n": lambda db, s, v, a: game.new_game(db, s, *(int(n) for n in a[:4])),
    "m": lambda db, s, v, a: game.move(db, int(a[0]), int(a[1]), int(a[2])),
    "a": lambda db, s, v, a: game.attack(db, int(a[0]), int(a[1]), int(a[2])),
    "b": lambda db, s, v, a: (game.buy(db, a[0], int(a[1]), int(a[2]))
                              if a else open_tray(v, "buy")),
    "c": lambda db, s, v, a: game.found_city(db, int(a[0]),
                                             " ".join(a[1:]) or "New City"),
    "t": lambda db, s, v, a: (game.set_research(db, a[0]) if a
                              else open_tray(v, "research")),
    "s": lambda db, s, v, a: select(db, s, v, int(a[0])),
    "e": lambda db, s, v, a: game.end_turn(db),
    "p": lambda db, s, v, a: (game.use_civ(db, s, int(a[0])) if a
                              else open_tray(v, "civs")),
    "v": lambda db, s, v, a: game.look_at(s, *(int(n) for n in a[:2])),
    "y": lambda db, s, v, a: typed_overlay(s, a),
    "?": lambda db, s, v, a: open_tray(v, "help"),
}


def open_tray(view, which):
    view.tray = None if view.tray == which else which
    view.placing = None


def submit(db, session, view, world, force_sql=False):
    """Run whatever is in the prompt.

    Clicks do not come through here and do not enter the history: re-running a
    statement to watch its result change is the teaching loop, and a history
    full of moves would bury the statements worth re-running.
    """
    text = im.edit_line(view.line, "enter", view.history)
    if not text:
        return False
    if view.history[-1:] != [text]:
        view.history.append(text)

    view.result = None
    if not force_sql and reading(text) == "command":
        verb, args = text[0], text[1:].split()
        return try_it(view, lambda: TYPED[verb](db, session, view, args))
    return try_it(view, lambda: keep_result(db, session, view, text))


def keep_result(db, session, view, sql):
    """Send typed SQL and hold on to whatever came back.

    Always logged, unlike the reads behind a redraw: a statement you typed is
    one you are watching.
    """
    view.result = game.run_sql(db, session, sql)


# Shift and a letter is a shortcut whichever way the prompt is; a bare letter
# is a character you can type. That is what lets shift+B open the shop while
# `b` still starts the command the terminal uses for the same thing.
SHORTCUT_TRAYS = {pygame.K_b: "buy", pygame.K_t: "research",
                  pygame.K_p: "civs", pygame.K_n: "new"}


def key_down(event, db, session, view, world):
    """One keypress.

    The prompt is always focused, so what a key does depends on whether there
    is anything in it: an empty prompt means the keyboard is driving the game,
    and text in the prompt means it is driving the text. Function keys and
    shift+letter shortcuts work either way.
    """
    shift = event.mod & pygame.KMOD_SHIFT
    ctrl = event.mod & pygame.KMOD_CTRL
    typing = bool(view.line["text"])

    if event.key in (pygame.K_F11, pygame.K_F12):
        wanted = "log" if event.key == pygame.K_F11 else "map"
        view.focus = "both" if view.focus == wanted else wanted
        return True, True
    if event.key == pygame.K_F1:
        open_tray(view, "help")
        return True, True

    if shift and event.key in SHORTCUT_TRAYS:
        open_tray(view, SHORTCUT_TRAYS[event.key])
        return True, True
    if shift and event.key == pygame.K_c and view.selected is not None:
        return True, try_it(view, lambda: game.found_city(db, view.selected))
    if shift and event.key == pygame.K_s and view.selected is not None:
        return True, try_it(view,
                            lambda: game.highlight_reachable(db, session,
                                                             view.selected))
    if shift and event.key == pygame.K_a and view.selected is not None \
            and view.hover:
        return True, act_on(db, session, view, world, *view.hover)

    if event.key in (pygame.K_RETURN, pygame.K_KP_ENTER):
        if shift:
            # Room for BEGIN; ... COMMIT; as one submission.
            view.line["text"] += "\n"
            return True, False
        return True, submit(db, session, view, world, force_sql=bool(ctrl))

    if event.key == pygame.K_BACKSPACE:
        im.edit_line(view.line, "backspace", view.history)
        return True, False

    if typing and event.key in (pygame.K_UP, pygame.K_DOWN):
        im.edit_line(view.line, "up" if event.key == pygame.K_UP else "down",
                     view.history)
        return True, False

    if event.key in PAN_KEYS and not typing:
        # Arrows step the selected unit, and pan when nothing is selected. One
        # keypress, one visible move_unit: the best way to narrate a move.
        if view.selected is not None and standing(world, view.selected):
            x, y = standing(world, view.selected)
            return True, act_on(db, session, view, world,
                                *(x + PAN_KEYS[event.key][0],
                                  y + PAN_KEYS[event.key][1]))
        game.pan(db, session, *PAN_KEYS[event.key])
        return True, True
    if event.key in PAN_KEYS:
        return True, False                     # left and right, while typing
    if event.key in ZOOM_KEYS and not typing:
        # `+` and `-` are printable, so they only zoom while the prompt is
        # empty. A statement rarely starts with either.
        view.zoom(ZOOM_KEYS[event.key])
        return True, True
    if event.key == pygame.K_HOME and not typing:
        game.look_at(session)                  # back to following your pieces
        return True, True
    if event.key == pygame.K_ESCAPE:
        # Escape drops the selection first, and only quits when there is
        # nothing left to drop -- twice, deliberately, and there is no quit
        # button. An accidental exit in front of a lecture hall is
        # unrecoverable theatre.
        if view.tray is not None or view.placing or view.result is not None:
            view.tray, view.placing, view.result = None, None, None
            return True, False
        if typing:
            view.line["text"], view.line["at"] = "", None
            return True, False
        if view.selected is not None or view.trouble:
            deselect(session, view)
            return True, False
        if view.escape_armed:
            return False, False
        view.escape_armed = True
        return True, False

    view.escape_armed = False

    if event.key == pygame.K_SPACE and not typing:
        deselect(session, view)
        return True, try_it(view, lambda: game.end_turn(db))

    if event.unicode and event.unicode.isprintable():
        im.edit_line(view.line, event.unicode, view.history)
        return True, False
    return True, False


def units_now(world):
    """Everyone's units as the last poll saw them, which is up to a fifth of a
    second old -- fine for deciding which statement to send, and never used to
    decide whether it is allowed."""
    return world["snap"]["units"] if world.get("snap") else ()


def standing(world, unit_id):
    """Where a unit is, or None if it is gone."""
    for unit in units_now(world):
        if unit["unit_id"] == unit_id:
            return unit["x"], unit["y"]
    return None


def occupant(world, x, y):
    """Whoever is on a tile, or None."""
    for unit in units_now(world):
        if (unit["x"], unit["y"]) == (x, y):
            return unit
    return None


def try_it(view, action):
    """Run one action, keeping whatever the database said about it.

    The refusal is the interesting output, so it is kept rather than logged and
    forgotten -- and a failed action still redraws, because being told no is a
    change to what is on screen.
    """
    view.trouble = ""
    try:
        action()
    except psycopg.Error as exc:
        view.trouble = str(exc).strip().splitlines()[0]
    except (ValueError, IndexError, KeyError) as exc:
        # A typed command with arguments it cannot use. Not the database's
        # doing, and not worth a traceback across a projector either.
        view.trouble = f"cannot read that: {exc}"
    return True


def select(db, session, view, unit_id):
    view.selected = unit_id
    view.trouble = ""
    game.highlight_reachable(db, session, unit_id)


def deselect(session, view):
    view.selected = None
    view.trouble = ""
    game.clear_highlight(session)


def click_chrome(db, session, view, world, pos):
    """Buttons and trays. Returns None if the click was not on any of them, so
    that the caller can offer it to the map instead."""
    bands = world["bands"]
    was_open = view.tray

    if view.tray is not None:
        _title, rows = tray_contents(view, world)
        rect = tray_rect(bands, rows)
        for key, line in tray_slots(rect, rows, bands.ui):
            if line.collidepoint(pos):
                return tray_chosen(db, session, view, world, key, line, pos)
        if rect.collidepoint(pos):
            return False                   # inside the tray, but on no row
        view.tray = None                   # clicking away closes it

    for key, slot in hud_slots(bands.hud, bands.ui).items():
        if not slot.collidepoint(pos):
            continue
        if key.startswith("overlay:"):
            which = key.removeprefix("overlay:")
            session.overlay = None if which == "terrain" else which
        else:
            # Against `was_open`, not view.tray: clicking away just cleared it,
            # so comparing with the current value would close this tray and
            # immediately reopen it.
            view.tray = None if was_open == key else key
            view.placing = None
        return True

    if world.get("snap"):
        for key, slot in context_slots(bands.context, world["snap"], view,
                                       bands.ui).items():
            if not slot.collidepoint(pos):
                continue
            if key == "end_turn":
                deselect(session, view)
                return try_it(view, lambda: game.end_turn(db))
            return try_it(view, lambda: game.found_city(db, view.selected))
    return None


def tray_chosen(db, session, view, world, key, line, pos):
    """One row of the open tray was clicked."""
    kind, _, value = key.partition(":")

    if kind == "buy":
        # The unit is handed to the cursor rather than bought on the spot:
        # buy_unit wants somewhere to put it, and picking that somewhere is a
        # click on the map. Locked units are still clickable, and buy_unit is
        # what says no.
        view.placing, view.tray, view.trouble = value, None, ""
        return True
    if kind == "tech":
        view.tray = None
        return try_it(view, lambda: game.set_research(db, value))
    if kind == "civ":
        view.tray = None
        deselect(session, view)
        game.use_civ(db, session, int(value))
        return True
    if kind == "field":
        # Left half steps down, right half steps up.
        low, high = next((lo, hi) for name, lo, hi in NEW_GAME_FIELDS
                         if name == value)
        step = -1 if pos[0] < line.centerx + line.width // 4 else 1
        if value == "seed":
            step *= 7                      # a seed of 43 looks like a seed of 42
        view.newgame[value] = max(low, min(high, view.newgame[value] + step))
        return True
    if kind == "deal":
        view.tray = None
        deselect(session, view)
        return try_it(view, lambda: game.new_game(db, session, **view.newgame))
    return False


def click(db, session, view, world, pos):
    """Click one of your units to pick it, then a square to send it there.

    The same two-phase gesture as the terminal's, where picking a unit leaves
    `m 3 ` in the prompt. A second click on a tile executes rather than typing
    it out, and a refused move keeps the selection so the next click can just
    be a better one.
    """
    board = world["board"]
    spot = board.tile_at(pos)
    if spot is None:
        return False

    if view.placing:
        kind, view.placing = view.placing, None
        return try_it(view, lambda: game.buy(db, kind, *spot))

    mine = game.my_unit_at(db, *spot, game.active_civ(db, session))
    if mine:
        select(db, session, view, mine)
        return True
    if view.selected is None:
        return False
    return act_on(db, session, view, world, *spot)


def act_on(db, session, view, world, x, y):
    """Send the selected unit at (x, y): a fight if someone is there, a walk if
    not. Which of the two is the only thing decided here; whether it is allowed
    is the database's business."""
    if view.selected is None:
        return False
    foe = occupant(world, x, y)
    enemy = foe is not None and foe["civ_id"] != world["snap"]["civ_id"]
    try_it(view, (lambda: game.attack(db, view.selected, x, y)) if enemy else
                 (lambda: game.move(db, view.selected, x, y)))

    # Whether it worked or not, where the unit can go has changed -- it spent
    # movement, or it is standing somewhere new, or it is dead.
    if standing(world, view.selected) is not None:
        try_it_quietly(db, session, view)
    return True


def try_it_quietly(db, session, view):
    """Re-light the reachable set, keeping any refusal already on screen."""
    said = view.trouble
    try:
        game.highlight_reachable(db, session, view.selected)
    except psycopg.Error:
        deselect(session, view)                # the unit died in the attempt
    view.trouble = said


def drag(db, session, view, rel):
    """Pan by whole tiles once a drag has covered one.

    Accumulating the remainder rather than rounding each event keeps a slow
    drag from losing ground, and dragging right pulls the world right, so the
    view moves left.
    """
    dx, dy = view.drag[0] + rel[0], view.drag[1] + rel[1]
    tiles = (-(dx // view.tile_px), dy // view.tile_px)
    view.drag = (dx % view.tile_px, dy % view.tile_px)
    if tiles == (0, 0):
        return False
    game.pan(db, session, *tiles)
    return True


def maybe_centre(db, session, view, board, pos):
    """Double-click a tile to centre the view on it.

    Both clicks have to land on the same tile. Without that, picking a unit and
    then clicking a square next to it inside a third of a second reads as a
    double-click and moves the camera instead of the unit -- which is exactly
    how fast anyone actually plays.
    """
    when, was = view.last_click
    spot = board.tile_at(pos)
    view.last_click = (time.monotonic(), spot)
    if spot is None or was != spot or time.monotonic() - when > DOUBLE_CLICK:
        return False
    game.look_at(session, *spot)
    return True


# ---------------------------------------------------------------------- loop

def main():
    pygame.init()
    pygame.display.set_caption("civ")
    surface = pygame.display.set_mode(DEFAULT_SIZE, pygame.RESIZABLE)
    clock = pygame.time.Clock()

    session = game.Session()
    view = View()
    try:
        db = DB(echo=session.log.append)
    except psycopg.OperationalError as exc:
        sys.exit(f"cannot reach the database: {exc}\n"
                 f"try: docker compose up -d && make reset")

    legend = game.terrain_legend(db)
    world, next_poll = {"bands": layout(surface.get_size()), "snap": None}, 0.0
    running = True
    named = None

    while running:
        stale = False
        for event in pygame.event.get():
            # One catch, around the whole handler rather than around each
            # action, so that no click can take the window down mid-lecture.
            # Actions already keep their own refusals; this is the backstop.
            try:
                running, dirty = handle(event, db, session, view, world)
            except psycopg.Error as exc:
                view.trouble = str(exc).strip().splitlines()[0]
                running, dirty = True, True
            stale = stale or dirty
            if not running:
                break

        now = time.monotonic()
        if stale or now >= next_poll:
            world = read_world(db, session, view, surface.get_size())
            next_poll = now + POLL_SECONDS
            # Two windows on one database want telling apart on the taskbar.
            if world["snap"] and world["snap"]["civ"]["name"] != named:
                named = world["snap"]["civ"]["name"]
                pygame.display.set_caption(f"civ -- {named}")

        view.ants = (view.ants + 1) % 1000     # one pixel a frame, forever
        draw(surface, world, session, view, legend, db)
        pygame.display.flip()
        clock.tick(60)

    pygame.quit()


if __name__ == "__main__":
    main()
