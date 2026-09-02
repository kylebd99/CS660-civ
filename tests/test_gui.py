"""The windowed front-end, with no window.

SDL's dummy driver gives a real framebuffer with nothing on the screen, so
these tests draw actual frames and then read pixels back out of them. That is
what makes the colour pipeline testable end to end: a number in 03_seed.sql
comes out as a pixel, and if either end of that chain moves, a test fails.

There are deliberately no golden images. They are flaky across freetype
versions and would fail on a student's Mac for reasons that have nothing to do
with this program. What is pinned instead is geometry, colour and cadence --
the parts that are actually decisions.
"""

import os

import pytest

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")   # before pygame.init()

import pygame                                       # noqa: E402

import db as dbapi                                  # noqa: E402
import game as core                                 # noqa: E402
import gui                                          # noqa: E402
import palette                                      # noqa: E402
import render                                       # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def pygame_ready():
    pygame.init()
    yield
    pygame.quit()


@pytest.fixture
def rome(game, dsn):
    """A session playing civ 1 on the world `game` dealt, with a city, a
    highlight and a spent unit -- one of each thing the map can draw."""
    session = core.Session()
    db = dbapi.DB(dsn=dsn, echo=session.log.append)
    core.use_civ(db, session, 1)
    core.found_city(db, 1, "Roma")
    core.highlight_reachable(db, session, 2)
    db.rows("UPDATE unit SET moves_left = 0, actions_left = 0 WHERE unit_id = 4")
    return db, session


@pytest.fixture
def frame(rome):
    """A drawn frame, and everything it was drawn from."""
    db, session = rome
    view = gui.View()
    world = gui.read_world(db, session, view, gui.DEFAULT_SIZE)
    surface = pygame.Surface(gui.DEFAULT_SIZE)
    legend = core.terrain_legend(db)
    gui.draw(surface, world, session, view, legend)
    return surface, world, session, view


# -------------------------------------------------------------------- layout

def test_the_bands_tile_the_window_without_gaps():
    bands = gui.layout(gui.DEFAULT_SIZE)
    width, height = gui.DEFAULT_SIZE
    assert bands.hud.top == 0 and bands.prompt.bottom == height
    # Down the left-hand side: hud, map, context, then the prompt.
    assert bands.hud.bottom == bands.map.top
    assert bands.map.bottom == bands.context.top
    assert bands.context.bottom == bands.prompt.top
    # The rail fills the height beside them and nothing overlaps it.
    assert bands.rail.top == bands.hud.bottom
    assert bands.rail.bottom == bands.prompt.top
    assert bands.rail.right == width
    assert not bands.rail.colliderect(bands.map)
    assert not bands.rail.colliderect(bands.context)


def test_the_reference_size_is_the_size_that_was_designed():
    """1600x1000 fits a 1080p projector, and the bands are the plan's."""
    bands = gui.layout((1600, 1000))
    assert bands.hud.height == 72
    assert bands.map.size == (1140, 700)
    assert bands.context.height == 150
    assert bands.rail.size == (460, 850)
    assert bands.prompt.height == 78
    assert bands.ui == 1.0


def test_a_narrow_window_puts_the_log_below_the_map():
    """Half width is how two players fit on one screen, and a long statement
    reads better across the window than down a column."""
    bands = gui.layout((900, 1000))
    assert bands.rail.width == 900
    assert bands.rail.top >= bands.map.bottom
    assert bands.map.width == 900


def test_the_bands_scale_with_the_window():
    small = gui.layout((1600, 500))
    assert small.ui == 0.5
    assert small.hud.height == 36
    assert small.prompt.bottom == 500


@pytest.mark.parametrize("focus, has_rail, has_map", [("both", True, True),
                                                      ("map", False, True),
                                                      ("log", True, True)])
def test_focus_gives_the_window_to_one_panel(focus, has_rail, has_map):
    bands = gui.layout(gui.DEFAULT_SIZE, focus)
    assert bool(bands.rail.height) is has_rail
    assert bool(bands.map.height) is has_map
    if focus == "log":
        # The map stays as a thumbnail, so the statements still have something
        # to be about, but the log gets the rest.
        assert bands.map.width < bands.rail.width
    if focus == "map":
        assert bands.map.width == gui.DEFAULT_SIZE[0]


# --------------------------------------------------------------------- board

@pytest.mark.parametrize("n, expected", [(1, 1), (2, 1), (26, 25), (27, 27),
                                         (0, 1), (-3, 1)])
def test_odd_rounds_down(n, expected):
    """window_for puts the same count of tiles either side of the centre, so an
    even request comes back one tile larger than asked for."""
    assert gui.odd(n) == expected


@pytest.mark.parametrize("size, focus", [((1600, 1000), "both"),
                                         ((900, 1000), "both"),
                                         ((1600, 1000), "map"),
                                         ((1600, 1000), "log"),
                                         ((640, 480), "both")])
def test_the_board_fits_the_band_it_was_asked_for(size, focus):
    bands = gui.layout(size, focus)
    board = gui.board_for(bands.map, (0, 0), gui.TILE_STEPS[1])
    x0, x1, y0, y1 = board.window
    assert board.area.size == ((x1 - x0 + 1) * board.tile_px,
                               (y1 - y0 + 1) * board.tile_px)
    assert bands.map.contains(board.area)


def test_every_tile_in_the_window_gets_exactly_one_rect():
    board = gui.board_for(gui.layout(gui.DEFAULT_SIZE).map, (8, 7),
                          gui.TILE_STEPS[1])
    x0, x1, y0, y1 = board.window
    rects = {(x, y): board.rect_of(x, y)
             for x in range(x0, x1 + 1) for y in range(y0, y1 + 1)}
    assert len(set(map(tuple, rects.values()))) == len(rects)
    assert all(board.area.contains(rect) for rect in rects.values())
    # They cover the board with nothing left over.
    assert sum(r.width * r.height for r in rects.values()) == \
        board.area.width * board.area.height


@pytest.mark.parametrize("tile_px", gui.TILE_STEPS)
def test_hit_testing_is_the_inverse_of_placement(tile_px):
    """The one loop that matters for clicking: every pixel of every tile has to
    come back as that tile, or a click lands one square out."""
    board = gui.board_for(gui.layout(gui.DEFAULT_SIZE).map, (8, 7), tile_px)
    x0, x1, y0, y1 = board.window
    for x in range(x0, x1 + 1):
        for y in range(y0, y1 + 1):
            rect = board.rect_of(x, y)
            for corner in (rect.center, rect.topleft,
                           (rect.right - 1, rect.bottom - 1)):
                assert board.tile_at(corner) == (x, y)


def test_a_click_outside_the_board_is_not_a_tile():
    bands = gui.layout(gui.DEFAULT_SIZE)
    board = gui.board_for(bands.map, (8, 7), gui.TILE_STEPS[1])
    assert board.tile_at((board.area.left - 1, board.area.centery)) is None
    assert board.tile_at(bands.rail.center) is None
    assert board.tile_at(bands.hud.center) is None
    # The coordinate gutter is outside the board on purpose, so that clicking
    # a tick does not move a unit.
    assert board.tile_at((board.area.centerx, board.area.bottom + 4)) is None


def test_the_view_centre_is_on_the_board(rome):
    db, session = rome
    board = gui.board_for(gui.layout(gui.DEFAULT_SIZE).map,
                          core.view_centre(db, session), gui.TILE_STEPS[1])
    x0, x1, y0, y1 = board.window
    # An odd board has a true centre tile, which is what `v` and Home mean.
    assert ((x0 + x1) // 2, (y0 + y1) // 2) == core.view_centre(db, session)


# ------------------------------------------------------------------- palette

@pytest.mark.parametrize("colour, expected", [
    (39, (0, 175, 255)),        # ocean, from 03_seed.sql
    (41, (0, 215, 95)),         # grass
    (149, (175, 215, 95)),      # plains
    (28, (0, 135, 0)),          # forest
    (137, (175, 135, 95)),      # hills
    (245, (138, 138, 138)),     # mountain, off the grey ramp
    (203, (255, 95, 95)),       # the first civ colour
    (16, (0, 0, 0)),            # both ends of the cube
    (231, (255, 255, 255)),
])
def test_the_palette_is_the_terminals_palette(colour, expected):
    assert palette.rgb(colour) == expected


def test_the_palette_is_total():
    assert all(len(palette.rgb(n)) == 3 for n in range(256))
    assert all(0 <= channel <= 255 for n in range(256) for channel in palette.rgb(n))


def test_ink_is_chosen_to_be_legible():
    assert palette.readable_on((255, 255, 255)) == (0, 0, 0)
    assert palette.readable_on((0, 0, 0)) == (255, 255, 255)
    # Rec. 601 rather than an average: pure blue is dark, pure green is not.
    assert palette.readable_on(palette.rgb(21)) == (255, 255, 255)
    assert palette.readable_on(palette.rgb(46)) == (0, 0, 0)


def test_dimming_moves_towards_black():
    assert palette.dim((200, 100, 50), 0.0) == (200, 100, 50)
    assert palette.dim((200, 100, 50), 1.0) == (0, 0, 0)
    assert palette.dim((200, 100, 50), 0.5) == (100, 50, 25)


# ---------------------------------------------------- pixels, and what drew them

def test_terrain_reaches_the_framebuffer_as_the_colour_the_seed_chose(frame):
    """The whole chain in one assertion: terrain.colour in 03_seed.sql, through
    map_window, through palette.rgb, onto a pixel."""
    surface, world, _, _ = frame
    board = world["board"]
    for cell in world["cells"][:40]:
        rect = board.rect_of(cell["x"], cell["y"])
        expected = palette.dim(palette.rgb(cell["colour"]), gui.TERRAIN_DIM)
        # A corner, not the centre: units and cities are drawn over the middle.
        assert surface.get_at((rect.left + 1, rect.top + 1))[:3] == expected


def test_units_are_drawn_over_their_tile(frame):
    surface, world, _, _ = frame
    board = world["board"]
    for unit in world["snap"]["units"]:
        rect = board.rect_of(unit["x"], unit["y"])
        terrain = palette.dim(palette.rgb(
            next(c["colour"] for c in world["cells"]
                 if (c["x"], c["y"]) == (unit["x"], unit["y"]))), gui.TERRAIN_DIM)
        assert surface.get_at(rect.center)[:3] != terrain


def test_a_unit_is_drawn_in_its_own_civs_colour(frame):
    surface, world, _, _ = frame
    board = world["board"]
    # Unit 2, the warrior: unit 1 was the settler and the fixture spent it on
    # founding Roma.
    mine = next(u for u in world["snap"]["units"] if u["unit_id"] == 2)
    rect = board.rect_of(mine["x"], mine["y"])
    body = surface.get_at((rect.centerx - round(rect.width * 0.2), rect.centery))
    assert body[:3] == palette.rgb(mine["colour"])


def test_a_spent_unit_is_greyed(frame):
    """Both budgets gone is the most useful glance in a turn-based game, so it
    cannot look the same as a unit that can still act."""
    surface, world, _, _ = frame
    board = world["board"]
    spent = next(u for u in world["snap"]["units"] if u["unit_id"] == 4)
    rect = board.rect_of(spent["x"], spent["y"])
    body = surface.get_at((rect.centerx - round(rect.width * 0.2), rect.centery))
    assert body[:3] != palette.rgb(spent["colour"])
    assert body[:3] == palette.dim(palette.rgb(spent["colour"]), 0.45)


def test_a_city_is_drawn_in_its_civs_colour(frame):
    surface, world, _, _ = frame
    city = world["snap"]["cities"][0]
    rect = world["board"].rect_of(city["x"], city["y"])
    assert surface.get_at((rect.centerx, rect.top + 6))[:3] == \
        palette.rgb(city["colour"])


def test_an_overlay_uses_the_same_heat_ramp_as_the_terminal(rome):
    """Both front-ends run render.HEAT through palette.rgb, so a food map is
    the same colour in a window as in a terminal."""
    db, session = rome
    session.overlay = "food"
    view = gui.View()
    world = gui.read_world(db, session, view, gui.DEFAULT_SIZE)
    surface = pygame.Surface(gui.DEFAULT_SIZE)
    gui.draw(surface, world, session, view, core.terrain_legend(db))

    assert world["highest"] is not None
    board = world["board"]
    for cell in world["cells"][:40]:
        rect = board.rect_of(cell["x"], cell["y"])
        expected = palette.rgb(render.heat(cell["value"], world["highest"]))
        assert surface.get_at((rect.left + 1, rect.top + 1))[:3] == expected


def test_a_window_with_no_world_says_so(dsn):
    db = dbapi.DB(dsn=dsn)
    db.rows("TRUNCATE world, civ CASCADE")
    session, view = core.Session(), gui.View()
    world = gui.read_world(db, session, view, gui.DEFAULT_SIZE)
    assert world["snap"] is None
    # It has to draw *something*, rather than falling over on the missing rows.
    surface = pygame.Surface(gui.DEFAULT_SIZE)
    gui.draw(surface, world, session, view)
    assert surface.get_at((0, 0))[:3] == gui.BACKDROP


def test_the_log_is_on_screen_and_wraps(frame):
    surface, world, session, view = frame
    assert session.log                              # the fixture made a city
    rail = world["bands"].rail
    long_one = "SELECT " + ", ".join(f"column_{n}" for n in range(40))
    lines = gui.wrap(long_one, rail.width - 32, 22)
    assert len(lines) > 1
    assert " ".join(lines).split() == long_one.split()   # nothing lost


# -------------------------------------------------------------------- cadence
# The world is polled, so the only thing that needs deciding is when a poll is
# forced instead of waited for. That answer is what `handle` returns.

def event(kind, **fields):
    return pygame.event.Event(kind, **fields)


@pytest.mark.parametrize("described, made, forces", [
    ("panning", event(pygame.KEYDOWN, key=pygame.K_LEFT), True),
    ("zooming", event(pygame.KEYDOWN, key=pygame.K_MINUS), True),
    ("following your pieces", event(pygame.KEYDOWN, key=pygame.K_HOME), True),
    ("filling the window with the log",
     event(pygame.KEYDOWN, key=pygame.K_F11), True),
    ("resizing", event(pygame.VIDEORESIZE, size=(800, 600), w=800, h=600), True),
    ("moving the mouse", event(pygame.MOUSEMOTION, pos=(500, 400), rel=(3, 3),
                               buttons=(0, 0, 0)), False),
    ("one press of escape", event(pygame.KEYDOWN, key=pygame.K_ESCAPE), False),
    ("a key that does nothing", event(pygame.KEYDOWN, key=pygame.K_z), False),
])
def test_only_what_changes_the_view_forces_a_re_read(described, made, forces,
                                                     rome):
    db, session = rome
    view = gui.View()
    world = gui.read_world(db, session, view, gui.DEFAULT_SIZE)
    running, stale = gui.handle(made, db, session, view, world)
    assert running, described
    assert stale is forces, described


def test_closing_the_window_stops_the_loop(rome):
    db, session = rome
    running, _ = gui.handle(event(pygame.QUIT), db, session, gui.View(), {})
    assert running is False


def test_escape_has_to_be_pressed_twice(rome):
    """There is no quit button either. An accidental exit in front of a lecture
    hall is unrecoverable theatre."""
    db, session = rome
    view = gui.View()
    escape = event(pygame.KEYDOWN, key=pygame.K_ESCAPE)
    assert gui.handle(escape, db, session, view, {})[0] is True
    assert gui.handle(escape, db, session, view, {})[0] is False


def test_any_other_key_disarms_escape(rome):
    db, session = rome
    view = gui.View()
    gui.handle(event(pygame.KEYDOWN, key=pygame.K_ESCAPE), db, session, view, {})
    gui.handle(event(pygame.KEYDOWN, key=pygame.K_z), db, session, view, {})
    assert gui.handle(event(pygame.KEYDOWN, key=pygame.K_ESCAPE),
                      db, session, view, {})[0] is True


def test_dragging_pans_by_whole_tiles_and_keeps_the_remainder(rome):
    db, session = rome
    view = gui.View()
    core.look_at(session, 10, 10)
    world = gui.read_world(db, session, view, gui.DEFAULT_SIZE)

    gui.handle(event(pygame.MOUSEBUTTONDOWN, button=3, pos=(500, 400)),
               db, session, view, world)
    assert view.panning
    # Half a tile is not yet a tile, but it is not thrown away either.
    half = view.tile_px // 2
    _, stale = gui.handle(event(pygame.MOUSEMOTION, pos=(500 + half, 400),
                                rel=(half, 0), buttons=(0, 0, 1)),
                          db, session, view, world)
    assert stale is False and session.view == (10, 10)
    _, stale = gui.handle(event(pygame.MOUSEMOTION, pos=(500 + 2 * half, 400),
                                rel=(half, 0), buttons=(0, 0, 1)),
                          db, session, view, world)
    # Dragging right pulls the world right, so the view moves left.
    assert stale is True and session.view == (9, 10)

    gui.handle(event(pygame.MOUSEBUTTONUP, button=3, pos=(500, 400)),
               db, session, view, world)
    assert not view.panning


def test_the_frame_costs_the_same_number_of_statements_on_a_bigger_world(rome):
    """The windowing is the point: a world can have a million tiles and a
    window can show a few hundred, so a redraw is an index range scan rather
    than a read of the world. demo/02-scale.sql is the same claim, measured.
    """
    db, session = rome
    view = gui.View()
    sent = []
    plain = db.rows
    db.rows = lambda sql, params=(): (sent.append(sql), plain(sql, params))[1]

    gui.read_world(db, session, view, gui.DEFAULT_SIZE)
    small = len(sent)

    core.new_game(db, session, 200, 120, 2, 42)
    core.active_civ(db, session)
    sent.clear()
    big = gui.read_world(db, session, view, gui.DEFAULT_SIZE)
    assert len(sent) <= small

    # 24000 tiles now exist and the window reads only the ones it can draw.
    # Not "the same count as the small world": that one was clipped by the edge
    # of a 30x16 map, and being clipped is the cheaper case, not the invariant.
    x0, x1, y0, y1 = big["board"].window
    assert len(big["cells"]) == (x1 - x0 + 1) * (y1 - y0 + 1) < 500


# --------------------------------------------------------------- playing it
# The plan's bar for this phase is playing a whole turn from the window, so
# these drive the real event handlers rather than the functions under them.

def click_on(db, session, view, world, board, spot, button=1):
    """Click the middle of a tile, as a person would."""
    return gui.handle(event(pygame.MOUSEBUTTONDOWN, button=button,
                            pos=board.rect_of(*spot).center),
                      db, session, view, world)


def test_clicking_your_own_unit_selects_it_and_lights_up_where_it_can_go(rome):
    db, session = rome
    view = gui.View()
    world = gui.read_world(db, session, view, gui.DEFAULT_SIZE)

    _, stale = click_on(db, session, view, world, world["board"], (7, 6))
    assert stale is True
    assert view.selected == 2
    assert session.highlight[(8, 6)] == 1              # cost, not just a tile
    assert session.log[-1] == "SELECT x, y, cost FROM reachable(2)"


def test_clicking_a_square_moves_the_selected_unit(rome):
    db, session = rome
    view = gui.View()
    world = gui.read_world(db, session, view, gui.DEFAULT_SIZE)
    board = world["board"]

    click_on(db, session, view, world, board, (7, 6))
    click_on(db, session, view, world, board, (8, 6))
    assert view.trouble == ""
    assert core.my_unit_at(db, 8, 6, 1) == 2
    assert "move_unit(2, 8, 6)" in " ".join(session.log)
    # Still selected, and the reachable set has shrunk with the movement spent.
    assert view.selected == 2
    assert max(session.highlight.values()) == 1


def test_a_refused_move_is_shown_and_keeps_the_selection(rome):
    """unit_one_per_tile rejecting a move is a lecture beat, not an error to be
    smoothed over -- and the client never pre-empts it."""
    db, session = rome
    view = gui.View()
    world = gui.read_world(db, session, view, gui.DEFAULT_SIZE)
    board = world["board"]

    click_on(db, session, view, world, board, (7, 6))
    click_on(db, session, view, world, board, (7, 7))   # ocean, and 1 mp away
    assert "cannot reach" in view.trouble
    assert view.selected == 2
    assert session.highlight                            # still lit, to retry
    assert core.my_unit_at(db, 7, 6, 1) == 2            # and it did not move


def test_clicking_off_the_map_does_nothing(rome):
    db, session = rome
    view = gui.View()
    world = gui.read_world(db, session, view, gui.DEFAULT_SIZE)
    click_on(db, session, view, world, world["board"], (7, 6))

    _, stale = gui.handle(event(pygame.MOUSEBUTTONDOWN, button=1,
                                pos=world["bands"].rail.center),
                          db, session, view, world)
    assert stale is False
    assert view.selected == 2                           # nothing was dropped


def test_arrows_step_the_selected_unit_and_pan_when_nothing_is(rome):
    """One keypress, one visible move_unit, which is what makes it good for
    narrating a move to a room."""
    db, session = rome
    view = gui.View()
    world = gui.read_world(db, session, view, gui.DEFAULT_SIZE)
    core.look_at(session, 7, 6)

    gui.handle(event(pygame.KEYDOWN, key=pygame.K_RIGHT), db, session, view, world)
    assert session.view == (8, 6)                       # panned, nothing picked

    click_on(db, session, view, world, world["board"], (7, 6))
    gui.handle(event(pygame.KEYDOWN, key=pygame.K_RIGHT), db, session, view, world)
    assert core.my_unit_at(db, 8, 6, 1) == 2
    assert session.view == (8, 6)                       # the camera stayed put


def test_clicking_an_enemy_attacks_it(rome):
    db, session = rome
    view = gui.View()
    # Stand their warrior next to ours; marching it eleven tiles is move_unit's
    # test, not this one.
    db.rows("""UPDATE unit SET tile_id = (SELECT tile_id FROM tile
                                          WHERE x = 8 AND y = 6)
               WHERE unit_id = 4""")
    world = gui.read_world(db, session, view, gui.DEFAULT_SIZE)
    board = world["board"]

    click_on(db, session, view, world, board, (7, 6))
    click_on(db, session, view, world, board, (8, 6))
    assert view.trouble == ""
    assert "attack(2, 8, 6)" in " ".join(session.log)
    assert "move_unit" not in " ".join(session.log)
    # It was a fight, so their warrior took damage and ours stayed put.
    assert db.one("SELECT hp FROM unit WHERE unit_id = 4")["hp"] < 20
    assert core.my_unit_at(db, 7, 6, 1) == 2


def test_attacking_something_out_of_reach_is_refused_not_prevented(rome):
    db, session = rome
    view = gui.View()
    world = gui.read_world(db, session, view, gui.DEFAULT_SIZE)
    board = world["board"]

    click_on(db, session, view, world, board, (7, 6))
    view.hover = (19, 9)                                # their warrior, far off
    gui.handle(event(pygame.KEYDOWN, key=pygame.K_a), db, session, view, world)
    assert "is not next to" in view.trouble


def test_c_founds_a_city_with_a_settler(game, dsn):
    session = core.Session()
    db = dbapi.DB(dsn=dsn, echo=session.log.append)
    core.use_civ(db, session, 1)
    view = gui.View()
    world = gui.read_world(db, session, view, gui.DEFAULT_SIZE)

    click_on(db, session, view, world, world["board"], (8, 7))   # the settler
    assert view.selected == 1
    gui.handle(event(pygame.KEYDOWN, key=pygame.K_c), db, session, view, world)
    assert view.trouble == ""
    assert core.snapshot(db, session)["my_cities"]


def test_c_with_a_warrior_says_what_is_wrong_with_that(rome):
    db, session = rome
    view = gui.View()
    world = gui.read_world(db, session, view, gui.DEFAULT_SIZE)
    click_on(db, session, view, world, world["board"], (7, 6))
    gui.handle(event(pygame.KEYDOWN, key=pygame.K_c), db, session, view, world)
    assert "cannot found cities" in view.trouble


def test_space_ends_the_turn(rome):
    db, session = rome
    view = gui.View()
    world = gui.read_world(db, session, view, gui.DEFAULT_SIZE)
    gui.handle(event(pygame.KEYDOWN, key=pygame.K_SPACE), db, session, view, world)
    assert core.snapshot(db, session)["world"]["turn"] == 2
    assert "end_turn()" in " ".join(session.log)


def test_escape_drops_the_selection_before_it_quits(rome):
    db, session = rome
    view = gui.View()
    world = gui.read_world(db, session, view, gui.DEFAULT_SIZE)
    click_on(db, session, view, world, world["board"], (7, 6))

    escape = event(pygame.KEYDOWN, key=pygame.K_ESCAPE)
    assert gui.handle(escape, db, session, view, world)[0] is True
    assert view.selected is None and session.highlight == {}
    # Only now does it start arming the quit.
    assert gui.handle(escape, db, session, view, world)[0] is True
    assert gui.handle(escape, db, session, view, world)[0] is False


def test_selecting_someone_elses_unit_selects_nothing(rome):
    db, session = rome
    view = gui.View()
    world = gui.read_world(db, session, view, gui.DEFAULT_SIZE)
    _, stale = click_on(db, session, view, world, world["board"], (19, 9))
    assert view.selected is None and stale is False


def test_the_selection_and_the_costs_are_drawn(rome):
    db, session = rome
    view = gui.View()
    world = gui.read_world(db, session, view, gui.DEFAULT_SIZE)
    board = world["board"]
    click_on(db, session, view, world, board, (7, 6))

    plain = pygame.Surface(gui.DEFAULT_SIZE)
    gui.draw(plain, world, session, view, {})
    view.selected = None
    core.clear_highlight(session)
    bare = pygame.Surface(gui.DEFAULT_SIZE)
    gui.draw(bare, world, session, view, {})

    rect = board.rect_of(7, 6)
    assert pygame.image.tobytes(plain.subsurface(rect), "RGB") != \
        pygame.image.tobytes(bare.subsurface(rect), "RGB")


def test_a_refusal_is_drawn_in_the_context_strip(rome):
    db, session = rome
    view = gui.View()
    world = gui.read_world(db, session, view, gui.DEFAULT_SIZE)
    view.trouble = "unit 2 cannot reach (7, 7) this turn"
    surface = pygame.Surface(gui.DEFAULT_SIZE)
    gui.draw(surface, world, session, view, {})

    strip = world["bands"].context
    pixels = {surface.get_at((x, y))[:3]
              for x in range(strip.left, strip.right, 3)
              for y in range(strip.top, strip.top + 40)}
    assert gui.ALARM in pixels


def test_double_clicking_one_tile_centres_on_it(rome):
    db, session = rome
    view = gui.View()
    world = gui.read_world(db, session, view, gui.DEFAULT_SIZE)
    board = world["board"]

    click_on(db, session, view, world, board, (12, 3))
    click_on(db, session, view, world, board, (12, 3))
    assert session.view == (12, 3)


def test_two_quick_clicks_on_different_tiles_are_two_clicks(rome):
    """Picking a unit and then clicking the square next to it takes far less
    than a third of a second, so proximity is what separates a double-click
    from a move -- not timing alone."""
    db, session = rome
    view = gui.View()
    world = gui.read_world(db, session, view, gui.DEFAULT_SIZE)
    board = world["board"]
    core.look_at(session, 7, 6)

    click_on(db, session, view, world, board, (7, 6))
    click_on(db, session, view, world, board, (8, 6))
    assert session.view == (7, 6)                  # the camera did not move
    assert core.my_unit_at(db, 8, 6, 1) == 2       # the unit did
