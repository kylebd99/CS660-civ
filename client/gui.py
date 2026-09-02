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
from dataclasses import dataclass

import psycopg
import pygame

import game
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
    last_click: tuple = (0.0, None)            # for double-click-to-centre

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
    return {"bands": bands, "snap": snap, "board": board,
            "cells": cells, "highest": highest}


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

    # The overlay's numbers, drawn big. A heat map you can only read as colour
    # is a decoration; with the value on it you can also read it as data.
    if world["highest"] is not None:
        for cell in world["cells"]:
            rect = board.rect_of(cell["x"], cell["y"])
            write(surface, cell["value"], rect.center, size=round(px * 0.6),
                  colour=palette.readable_on(fill[(cell["x"], cell["y"])]),
                  bold=True, anchor="center")

    # Where the selected unit could walk. A wash rather than an outline per
    # tile, so a 16-tile reachable set reads as one shape.
    for spot in session.highlight:
        rect = board.rect_of(*spot)
        if board.area.contains(rect):
            wash = pygame.Surface(rect.size, pygame.SRCALPHA)
            wash.fill((*INK, 64))
            surface.blit(wash, rect)
            pygame.draw.rect(surface, INK, rect.inflate(-4, -4), 1)

    for city in world["snap"]["cities"]:
        rect = board.rect_of(city["x"], city["y"]).inflate(-px // 6, -px // 6)
        colour = palette.rgb(city["colour"])
        pygame.draw.rect(surface, colour, rect, border_radius=px // 6)
        pygame.draw.rect(surface, INK, rect, 2, border_radius=px // 6)
        write(surface, city["population"], rect.center, size=round(px * 0.55),
              colour=palette.readable_on(colour), bold=True, anchor="center")
        write(surface, city["name"], (rect.centerx, rect.bottom + 2),
              size=round(px * 0.4), colour=INK, anchor="midtop")

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

    if view.hover is not None:
        hovered = board.rect_of(*view.hover)
        if board.area.contains(hovered):
            pygame.draw.rect(surface, INK, hovered, 2)


def draw_gutter(surface, board, ui):
    """Coordinates in a margin, ticked every five tiles.

    This replaces the terminal's corner captions, which had to be drawn on top
    of terrain and dropped when two of them collided. A gutter has room, so the
    collision logic is gone rather than ported.
    """
    x0, x1, y0, y1 = board.window
    size = max(11, round(15 * ui))
    for x in range(x0 - x0 % 5, x1 + 1, 5):
        rect = board.rect_of(x, y0)
        write(surface, x, (rect.centerx, board.area.bottom + 4), size=size,
              colour=MUTED, anchor="midtop")
    for y in range(y0 - y0 % 5, y1 + 1, 5):
        rect = board.rect_of(x0, y)
        write(surface, y, (board.area.left - 5, rect.centery), size=size,
              colour=MUTED, anchor="midright")


def draw_hud(surface, rect, snap, session, ui):
    """Turn, whose window this is, the purse, and what is being researched."""
    if not rect.height:
        return
    surface.fill(PANEL, rect)
    pygame.draw.line(surface, EDGE, rect.bottomleft, rect.bottomright)

    civ = snap["civ"]
    pad = round(18 * ui)
    big, small = round(30 * ui), round(21 * ui)
    at = write(surface, f"turn {snap['world']['turn']}",
               (rect.left + pad, rect.centery), size=big, bold=True,
               anchor="midleft")

    # The civ chip is filled with civ.colour, so in a two-window demo you can
    # tell at a glance which window is Rome without reading anything.
    colour = palette.rgb(civ["colour"])
    chip = pygame.Rect(at.right + pad, 0, round(150 * ui), round(34 * ui))
    chip.centery = rect.centery
    pygame.draw.rect(surface, colour, chip, border_radius=round(6 * ui))
    write(surface, civ["name"], chip.center, size=small,
          colour=palette.readable_on(colour), bold=True, anchor="center")

    at = write(surface, f"{civ['gold']}g   {civ['science']} science",
               (chip.right + pad, rect.centery), size=small, anchor="midleft")

    researching = civ["researching"]
    write(surface, researching or "researching nothing",
          (at.right + pad, rect.centery), size=small,
          colour=INK if researching else FAINT, anchor="midleft")


def draw_context(surface, rect, snap, view, world, legend, ui):
    """What the cursor is over, and what your units have left to spend."""
    if not rect.height:
        return
    surface.fill(PANEL, rect)
    pygame.draw.line(surface, EDGE, rect.topleft, rect.topright)

    pad = round(16 * ui)
    size = round(22 * ui)
    line = rect.top + pad

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
        if at > rect.right - round(200 * ui):        # wrap rather than run off
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


def draw_prompt(surface, rect, ui):
    """A placeholder until there is something to type into it."""
    if not rect.height:
        return
    surface.fill(PANEL, rect)
    pygame.draw.line(surface, EDGE, rect.topleft, rect.topright)
    write(surface, "arrows or drag to pan   Home follows your pieces   "
                   "+/- zoom   F11 log   F12 map   Esc Esc quits",
          (rect.left + round(16 * ui), rect.centery), size=round(19 * ui),
          colour=FAINT, anchor="midleft")


def draw(surface, world, session, view, legend=None):
    """One frame, from the rows the last poll fetched."""
    bands = world["bands"]
    legend = legend or {}
    surface.fill(BACKDROP)

    if world["snap"] is None:
        write(surface, "No world yet.", surface.get_rect().center,
              size=round(40 * bands.ui), bold=True, anchor="midbottom")
        write(surface, "Deal one with `make reset`, or `n` in the terminal "
                       "client.", surface.get_rect().center,
              size=round(22 * bands.ui), colour=MUTED, anchor="midtop")
        return

    draw_board(surface, world["board"], world, session, view)
    draw_gutter(surface, world["board"], bands.ui)
    draw_hud(surface, bands.hud, world["snap"], session, bands.ui)
    draw_context(surface, bands.context, world["snap"], view, world, legend,
                 bands.ui)
    draw_rail(surface, bands.rail, session.log, bands.ui)
    draw_prompt(surface, bands.prompt, bands.ui)


# --------------------------------------------------------------------- input
# Read-only for now: everything here moves the camera or the window. Returns
# True to keep the loop going, and True from `stale` to force a re-read rather
# than waiting out the poll -- latency is only felt on your own actions.

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
        return key_down(event, db, session, view)

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
            return True, maybe_centre(db, session, view, board, event.pos)

    if event.type == pygame.MOUSEBUTTONUP and event.button in (2, 3):
        view.panning = False

    return True, False


def key_down(event, db, session, view):
    if event.key in PAN_KEYS:
        game.pan(db, session, *PAN_KEYS[event.key])
        return True, True
    if event.key in ZOOM_KEYS:
        view.zoom(ZOOM_KEYS[event.key])
        return True, True
    if event.key == pygame.K_HOME:
        game.look_at(session)                  # back to following your pieces
        return True, True
    if event.key in (pygame.K_F11, pygame.K_F12):
        wanted = "log" if event.key == pygame.K_F11 else "map"
        view.focus = "both" if view.focus == wanted else wanted
        return True, True
    if event.key == pygame.K_ESCAPE:
        # Twice, deliberately, and there is no quit button. An accidental exit
        # in front of a lecture hall is unrecoverable theatre.
        if view.escape_armed:
            return False, False
        view.escape_armed = True
        return True, False
    view.escape_armed = False
    return True, False


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
    """Double-click to centre. A single left click is the selection gesture and
    does nothing yet."""
    now = time.monotonic()
    was_at, when = view.last_click[1], view.last_click[0]
    view.last_click = (now, pos)
    if was_at is None or now - when > DOUBLE_CLICK:
        return False
    spot = board.tile_at(pos)
    if spot is None:
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
            # action. A refused click has to leave the window standing.
            try:
                running, dirty = handle(event, db, session, view, world)
            except psycopg.Error as exc:
                session.log.append(str(exc).strip())
                running, dirty = True, False
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

        draw(surface, world, session, view, legend)
        pygame.display.flip()
        clock.tick(60)

    pygame.quit()


if __name__ == "__main__":
    main()
