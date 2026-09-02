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
    """A synthetic event.

    Real KEYDOWN events always carry `mod` and `unicode`, so they are filled in
    here rather than in every test -- and rather than making gui.py defensive
    about fields pygame guarantees.
    """
    if kind == pygame.KEYDOWN:
        fields = {"mod": 0, "unicode": "", **fields}
    return pygame.event.Event(kind, **fields)


def shift(key, unicode=""):
    return event(pygame.KEYDOWN, key=key, mod=pygame.KMOD_SHIFT,
                 unicode=unicode)


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
    gui.handle(shift(pygame.K_a), db, session, view, world)
    assert "is not next to" in view.trouble


def test_shift_c_founds_a_city_with_a_settler(game, dsn):
    session = core.Session()
    db = dbapi.DB(dsn=dsn, echo=session.log.append)
    core.use_civ(db, session, 1)
    view = gui.View()
    world = gui.read_world(db, session, view, gui.DEFAULT_SIZE)

    click_on(db, session, view, world, world["board"], (8, 7))   # the settler
    assert view.selected == 1
    gui.handle(shift(pygame.K_c), db, session, view, world)
    assert view.trouble == ""
    assert core.snapshot(db, session)["my_cities"]


def test_shift_c_with_a_warrior_says_what_is_wrong_with_that(rome):
    db, session = rome
    view = gui.View()
    world = gui.read_world(db, session, view, gui.DEFAULT_SIZE)
    click_on(db, session, view, world, world["board"], (7, 6))
    gui.handle(shift(pygame.K_c), db, session, view, world)
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


# ------------------------------------------------------------ chrome, trays
# The rule these check is that a button is enabled by something the database
# publishes -- unit_shop.unlocked, unit_type.founds_cities -- and that where
# there is no such column, the click goes out anyway and the refusal explains.

def click_at(db, session, view, world, pos):
    return gui.handle(event(pygame.MOUSEBUTTONDOWN, button=1, pos=pos),
                      db, session, view, world)


def slot(world, name):
    return gui.hud_slots(world["bands"].hud, world["bands"].ui)[name]


def test_the_hud_buttons_and_overlay_tabs_all_have_somewhere_to_be():
    bands = gui.layout(gui.DEFAULT_SIZE)
    slots = gui.hud_slots(bands.hud, bands.ui)
    assert set(slots) == {"new", "buy", "research", "civs", "help",
                          "overlay:terrain", "overlay:food",
                          "overlay:production", "overlay:gold"}
    assert all(bands.hud.contains(rect) for rect in slots.values())
    # Nothing overlaps anything else, or one button would eat another's clicks.
    rects = list(slots.values())
    assert not any(a.colliderect(b) for i, a in enumerate(rects)
                   for b in rects[i + 1:])


def test_the_overlay_tabs_switch_the_map_and_report_the_state(rome):
    db, session = rome
    view = gui.View()
    world = gui.read_world(db, session, view, gui.DEFAULT_SIZE)

    click_at(db, session, view, world, slot(world, "overlay:food").center)
    assert session.overlay == "food"
    world = gui.read_world(db, session, view, gui.DEFAULT_SIZE)
    assert world["highest"] is not None            # the map is now a heat map

    click_at(db, session, view, world, slot(world, "overlay:terrain").center)
    assert session.overlay is None


def test_a_hud_button_opens_its_tray_and_closes_it_again(rome):
    db, session = rome
    view = gui.View()
    world = gui.read_world(db, session, view, gui.DEFAULT_SIZE)

    click_at(db, session, view, world, slot(world, "buy").center)
    assert view.tray == "buy"
    world = gui.read_world(db, session, view, gui.DEFAULT_SIZE)
    assert world["shop"]                            # read only while it is open
    click_at(db, session, view, world, slot(world, "buy").center)
    assert view.tray is None
    assert "shop" not in gui.read_world(db, session, view, gui.DEFAULT_SIZE)


def test_the_shop_shows_locked_units_with_what_they_need(rome):
    db, session = rome
    view = gui.View()
    view.tray = "buy"
    world = gui.read_world(db, session, view, gui.DEFAULT_SIZE)
    _title, rows = gui.tray_contents(view, world)
    by_code = {label: row for row in rows for label in [row[1]]}

    assert by_code["warrior"][4] is True
    # unit_shop.unlocked is the database's answer, and the note is its reason.
    assert by_code["knight"][4] is False
    assert by_code["knight"][3] == "needs bronze_working"


def test_buying_hands_the_unit_to_the_cursor_and_the_map_places_it(rome):
    """Two clicks, and the second one is the statement: buy_unit wants
    somewhere to put the thing, and picking that somewhere is a click."""
    db, session = rome
    view = gui.View()
    db.rows("UPDATE civ SET gold = 500 WHERE civ_id = 1")
    view.tray = "buy"
    world = gui.read_world(db, session, view, gui.DEFAULT_SIZE)

    rect = gui.tray_rect(world["bands"], gui.tray_contents(view, world)[1])
    rows = dict(gui.tray_slots(rect, gui.tray_contents(view, world)[1],
                              world["bands"].ui))
    click_at(db, session, view, world, rows["buy:warrior"].center)
    assert view.placing == "warrior" and view.tray is None

    # Next to Roma, which the fixture founded on the settler's tile.
    world = gui.read_world(db, session, view, gui.DEFAULT_SIZE)
    click_at(db, session, view, world, world["board"].rect_of(8, 8).center)
    assert view.trouble == ""
    assert view.placing is None
    assert "buy_unit('warrior', 8, 8)" in " ".join(session.log)
    assert core.my_unit_at(db, 8, 8, 1) is not None


def test_placing_a_unit_somewhere_illegal_is_refused_not_prevented(rome):
    """Illegal tiles are deliberately not greyed out. buy_unit knows where a
    unit may stand and says so in a sentence."""
    db, session = rome
    view = gui.View()
    db.rows("UPDATE civ SET gold = 500 WHERE civ_id = 1")
    view.placing = "warrior"
    world = gui.read_world(db, session, view, gui.DEFAULT_SIZE)

    # Dry land, empty, and too far from Roma -- so the refusal is about the
    # city rather than about the sea.
    far = db.one("""SELECT t.x, t.y FROM tile t
                    JOIN terrain te ON te.code = t.terrain
                    JOIN city c ON true JOIN tile ct ON ct.tile_id = c.tile_id
                    WHERE te.passable AND distance(t.x, t.y, ct.x, ct.y) > 1
                      AND NOT EXISTS (SELECT 1 FROM unit u
                                      WHERE u.tile_id = t.tile_id)
                    ORDER BY t.x, t.y LIMIT 1""")
    click_at(db, session, view, world,
             world["board"].rect_of(far["x"], far["y"]).center)
    assert "not next to a city of yours" in view.trouble

    # And the sea has its own answer, which is also the database's.
    view.placing = "warrior"
    click_at(db, session, view, world, world["board"].rect_of(7, 7).center)
    assert "not somewhere a unit can stand" in view.trouble


def test_the_research_tray_offers_what_available_tech_offers(rome):
    db, session = rome
    view = gui.View()
    view.tray = "research"
    world = gui.read_world(db, session, view, gui.DEFAULT_SIZE)
    _title, rows = gui.tray_contents(view, world)
    assert {row[1] for row in rows} == {t["code"] for t in world["techs"]}
    assert "agriculture" in {row[1] for row in rows}
    # currency needs bronze_working, so the frontier does not include it yet.
    assert "currency" not in {row[1] for row in rows}

    rect = gui.tray_rect(world["bands"], rows)
    slots = dict(gui.tray_slots(rect, rows, world["bands"].ui))
    click_at(db, session, view, world, slots["tech:agriculture"].center)
    assert view.tray is None
    assert core.snapshot(db, session)["civ"]["researching"] == "agriculture"


def test_the_civ_tray_switches_which_player_this_window_is(rome):
    """Its set_config statement has to appear in the log: that statement *is*
    the session-identity lecture."""
    db, session = rome
    view = gui.View()
    view.tray = "civs"
    world = gui.read_world(db, session, view, gui.DEFAULT_SIZE)
    rows = gui.tray_contents(view, world)[1]
    slots = dict(gui.tray_slots(gui.tray_rect(world["bands"], rows), rows,
                                world["bands"].ui))

    click_at(db, session, view, world, slots["civ:2"].center)
    assert session.civ_id == 2
    assert session.log[-1] == "SELECT set_config('app.civ_id', '2', false)"
    assert db.one("SELECT current_civ() AS me")["me"] == 2


def test_the_new_game_spinners_step_and_deal(rome):
    db, session = rome
    view = gui.View()
    view.tray = "new"
    world = gui.read_world(db, session, view, gui.DEFAULT_SIZE)
    rows = gui.tray_contents(view, world)[1]
    slots = dict(gui.tray_slots(gui.tray_rect(world["bands"], rows), rows,
                                world["bands"].ui))

    civs = slots["field:civs"]
    click_at(db, session, view, world, (civs.right - 8, civs.centery))
    assert view.newgame["civs"] == 3
    click_at(db, session, view, world, (civs.left + 8, civs.centery))
    assert view.newgame["civs"] == 2
    # And it cannot be stepped below one civ, which new_game refuses anyway.
    for _ in range(5):
        click_at(db, session, view, world, (civs.left + 8, civs.centery))
    assert view.newgame["civs"] == 1

    view.newgame.update(width=20, height=12, seed=7)
    click_at(db, session, view, world, slots["deal"].center)
    assert view.tray is None
    assert "new_game(20, 12, 7, 1)" in " ".join(session.log)
    assert core.snapshot(db, session)["world"]["width"] == 20


def test_found_city_appears_only_for_a_unit_that_can_found_one(game, dsn):
    """unit_type.founds_cities and an action in hand -- both already on the row
    the map was drawn from, so this is a column read, not a rule known."""
    session = core.Session()
    db = dbapi.DB(dsn=dsn, echo=session.log.append)
    core.use_civ(db, session, 1)
    view = gui.View()
    world = gui.read_world(db, session, view, gui.DEFAULT_SIZE)
    bands = world["bands"]

    def buttons():
        return set(gui.context_slots(bands.context, world["snap"], view,
                                     bands.ui))

    assert buttons() == {"end_turn"}                # nothing selected
    view.selected = 2                               # the warrior
    assert buttons() == {"end_turn"}
    view.selected = 1                               # the settler
    assert buttons() == {"end_turn", "found"}

    found = gui.context_slots(bands.context, world["snap"], view,
                              bands.ui)["found"]
    click_at(db, session, view, world, found.center)
    assert view.trouble == ""
    assert core.snapshot(db, session)["my_cities"]

    # Spent, so the button goes away rather than offering a refusal.
    world = gui.read_world(db, session, view, gui.DEFAULT_SIZE)
    assert buttons() == {"end_turn"}


def test_the_end_turn_button_ends_the_turn(rome):
    db, session = rome
    view = gui.View()
    world = gui.read_world(db, session, view, gui.DEFAULT_SIZE)
    bands = world["bands"]
    end = gui.context_slots(bands.context, world["snap"], view, bands.ui)["end_turn"]

    click_at(db, session, view, world, end.center)
    assert core.snapshot(db, session)["world"]["turn"] == 2


def test_clicking_the_map_through_an_open_tray_does_not_move_anything(rome):
    """A tray is over the map, so its own rectangle has to swallow clicks."""
    db, session = rome
    view = gui.View()
    view.tray = "buy"
    world = gui.read_world(db, session, view, gui.DEFAULT_SIZE)
    rows = gui.tray_contents(view, world)[1]
    rect = gui.tray_rect(world["bands"], rows)

    click_at(db, session, view, world, (rect.centerx, rect.bottom - 4))
    assert view.selected is None and view.tray == "buy"


def test_clicking_the_map_away_from_a_tray_closes_it(rome):
    db, session = rome
    view = gui.View()
    view.tray = "research"
    world = gui.read_world(db, session, view, gui.DEFAULT_SIZE)
    click_at(db, session, view, world, world["board"].rect_of(7, 6).center)
    assert view.tray is None


def test_escape_closes_a_tray_before_it_touches_the_selection(rome):
    db, session = rome
    view = gui.View()
    world = gui.read_world(db, session, view, gui.DEFAULT_SIZE)
    click_on(db, session, view, world, world["board"], (7, 6))
    view.tray = "buy"

    gui.handle(event(pygame.KEYDOWN, key=pygame.K_ESCAPE), db, session, view, world)
    assert view.tray is None and view.selected == 2


@pytest.mark.parametrize("key, tray", [(pygame.K_b, "buy"),
                                       (pygame.K_t, "research"),
                                       (pygame.K_p, "civs"),
                                       (pygame.K_n, "new")])
def test_shift_and_the_terminals_letter_opens_the_matching_tray(key, tray, rome):
    """The same letters the terminal uses, so lecture notes survive -- with
    shift, because a bare letter is a character the prompt can have."""
    db, session = rome
    view = gui.View()
    world = gui.read_world(db, session, view, gui.DEFAULT_SIZE)
    gui.handle(shift(key), db, session, view, world)
    assert view.tray == tray


def test_every_tray_draws_without_a_world_row_it_does_not_have(rome):
    """Each tray reads its own rows, so opening one whose rows have not been
    fetched has to draw empty rather than fall over."""
    db, session = rome
    view = gui.View()
    surface = pygame.Surface(gui.DEFAULT_SIZE)
    for tray in ("buy", "research", "civs", "new", "help"):
        view.tray = tray
        world = gui.read_world(db, session, view, gui.DEFAULT_SIZE)
        world.pop("shop", None), world.pop("techs", None), world.pop("civs", None)
        gui.draw(surface, world, session, view, {})


# --------------------------------------------------------------- the prompt
# It takes both the terminal's one-letter commands and raw SQL, which is what
# lets a lecture's notes work in either front-end.

def typing(db, session, view, world, text, mod=0):
    for character in text:
        gui.handle(event(pygame.KEYDOWN, key=ord(character[0]) if character
                         else 0, unicode=character), db, session, view, world)
    return gui.handle(event(pygame.KEYDOWN, key=pygame.K_RETURN, mod=mod),
                      db, session, view, world)


@pytest.mark.parametrize("text, taken_as", [
    ("e", "command"),
    ("m 2 8 6", "command"),
    ("y food", "command"),
    ("SELECT * FROM unit", "SQL"),
    ("select 1", "SQL"),                       # `s` is a command, `se` is not
    ("BEGIN;", "SQL"),
    ("", None),
    ("   ", None),
])
def test_the_prompt_says_which_of_the_two_it_is_reading(text, taken_as):
    assert gui.reading(text) == taken_as


def test_typing_a_command_runs_it(rome):
    db, session = rome
    view = gui.View()
    world = gui.read_world(db, session, view, gui.DEFAULT_SIZE)

    typing(db, session, view, world, "m 2 8 6")
    assert view.trouble == ""
    assert core.my_unit_at(db, 8, 6, 1) == 2
    assert view.line["text"] == ""              # and the prompt is clear again


def test_typing_sql_runs_it_and_keeps_the_rows(rome):
    db, session = rome
    view = gui.View()
    world = gui.read_world(db, session, view, gui.DEFAULT_SIZE)

    typing(db, session, view, world, "SELECT name, gold FROM civ ORDER BY civ_id")
    assert view.trouble == ""
    assert [row["name"] for row in view.result] == ["Rome", "Carthage"]
    # Typed statements are always logged, unlike the reads behind a redraw.
    assert session.log[-1] == "SELECT name, gold FROM civ ORDER BY civ_id"


def test_ctrl_enter_forces_sql(rome):
    """`e` is the end-turn command and also the start of a great many
    statements, so there has to be a way to insist."""
    db, session = rome
    view = gui.View()
    world = gui.read_world(db, session, view, gui.DEFAULT_SIZE)
    before = core.snapshot(db, session)["world"]["turn"]

    typing(db, session, view, world, "e", mod=pygame.KMOD_CTRL)
    assert core.snapshot(db, session)["world"]["turn"] == before
    assert view.trouble                         # `e` is not valid SQL


def test_a_command_with_the_wrong_arguments_says_so_rather_than_crashing(rome):
    db, session = rome
    view = gui.View()
    world = gui.read_world(db, session, view, gui.DEFAULT_SIZE)
    typing(db, session, view, world, "m 2")
    assert "cannot read that" in view.trouble


def test_bad_sql_shows_what_postgres_said(rome):
    db, session = rome
    view = gui.View()
    world = gui.read_world(db, session, view, gui.DEFAULT_SIZE)
    typing(db, session, view, world, "SELECT * FROM nonesuch")
    assert "nonesuch" in view.trouble
    assert view.result is None


def test_the_history_is_typed_lines_only(rome):
    """Re-running a statement to watch its result change is the teaching loop.
    A history full of clicked moves would bury the statements worth re-running.
    """
    db, session = rome
    view = gui.View()
    world = gui.read_world(db, session, view, gui.DEFAULT_SIZE)

    typing(db, session, view, world, "SELECT 1 AS a")
    click_on(db, session, view, world, world["board"], (7, 6))
    click_on(db, session, view, world, world["board"], (8, 6))
    assert view.history == ["SELECT 1 AS a"]

    # Up walks it back, exactly as the terminal's does.
    gui.handle(event(pygame.KEYDOWN, key=pygame.K_UP, unicode=""),
               db, session, view, world)
    assert view.line["text"] == ""              # nothing typed yet, so no walk
    gui.handle(event(pygame.KEYDOWN, key=pygame.K_x, unicode="x"),
               db, session, view, world)
    gui.handle(event(pygame.KEYDOWN, key=pygame.K_UP, unicode=""),
               db, session, view, world)
    assert view.line["text"] == "SELECT 1 AS a"
    gui.handle(event(pygame.KEYDOWN, key=pygame.K_DOWN, unicode=""),
               db, session, view, world)
    assert view.line["text"] == "x"             # the draft comes back


def test_an_empty_prompt_drives_the_game_and_a_full_one_drives_the_text(rome):
    db, session = rome
    view = gui.View()
    world = gui.read_world(db, session, view, gui.DEFAULT_SIZE)
    core.look_at(session, 10, 10)

    # Empty: space ends the turn and the arrows pan.
    gui.handle(event(pygame.KEYDOWN, key=pygame.K_RIGHT), db, session, view, world)
    assert session.view == (11, 10)

    gui.handle(event(pygame.KEYDOWN, key=pygame.K_s, unicode="s"),
               db, session, view, world)
    assert view.line["text"] == "s"
    # Full: the same arrow walks history instead of the map.
    gui.handle(event(pygame.KEYDOWN, key=pygame.K_RIGHT), db, session, view, world)
    assert session.view == (11, 10)
    # And a space is a space.
    gui.handle(event(pygame.KEYDOWN, key=pygame.K_SPACE, unicode=" "),
               db, session, view, world)
    assert view.line["text"] == "s "
    assert core.snapshot(db, session)["world"]["turn"] == 1


def test_backspace_and_escape_clear_what_you_typed(rome):
    db, session = rome
    view = gui.View()
    world = gui.read_world(db, session, view, gui.DEFAULT_SIZE)
    for char in "sel":
        gui.handle(event(pygame.KEYDOWN, key=ord(char), unicode=char),
                   db, session, view, world)
    gui.handle(event(pygame.KEYDOWN, key=pygame.K_BACKSPACE), db, session, view,
               world)
    assert view.line["text"] == "se"
    gui.handle(event(pygame.KEYDOWN, key=pygame.K_ESCAPE), db, session, view,
               world)
    assert view.line["text"] == ""
    # And having cleared the line it has not started arming the quit.
    assert gui.handle(event(pygame.KEYDOWN, key=pygame.K_ESCAPE), db, session,
                      view, world)[0] is True


def test_shift_enter_makes_room_for_a_transaction(rome):
    db, session = rome
    view = gui.View()
    world = gui.read_world(db, session, view, gui.DEFAULT_SIZE)
    for char in "BEGIN;":
        gui.handle(event(pygame.KEYDOWN, key=ord(char.lower()), unicode=char),
                   db, session, view, world)
    gui.handle(event(pygame.KEYDOWN, key=pygame.K_RETURN,
                     mod=pygame.KMOD_SHIFT), db, session, view, world)
    assert view.line["text"] == "BEGIN;\n"
    assert core.snapshot(db, session)["world"]["turn"] == 1   # not sent yet


def test_the_sigil_reports_the_connections_own_transaction_state(rome):
    """Without this a typed BEGIN changes the connection silently, which for
    the concurrency lectures is a defect in the teaching."""
    db, session = rome
    view = gui.View()
    world = gui.read_world(db, session, view, gui.DEFAULT_SIZE)
    assert db.in_transaction() is False

    typing(db, session, view, world, "BEGIN")
    assert db.in_transaction() is True
    surface = pygame.Surface(gui.DEFAULT_SIZE)
    gui.draw(surface, world, session, view, {}, db)
    band = world["bands"].prompt
    amber = {surface.get_at((x, y))[:3]
             for x in range(band.left, band.left + 120)
             for y in range(band.top, band.bottom, 2)}
    assert gui.AMBER in amber

    typing(db, session, view, world, "ROLLBACK")
    assert db.in_transaction() is False


def test_a_result_drawer_is_drawn_over_the_map_and_escape_closes_it(rome):
    db, session = rome
    view = gui.View()
    world = gui.read_world(db, session, view, gui.DEFAULT_SIZE)
    typing(db, session, view, world, "SELECT * FROM unit_type ORDER BY code")
    assert len(view.result) == 4

    surface = pygame.Surface(gui.DEFAULT_SIZE)
    bare = pygame.Surface(gui.DEFAULT_SIZE)
    gui.draw(surface, world, session, view, {}, db)
    view.result = None
    gui.draw(bare, world, session, view, {}, db)
    band = world["bands"].map
    assert pygame.image.tobytes(surface.subsurface(band), "RGB") != \
        pygame.image.tobytes(bare.subsurface(band), "RGB")


def test_explain_comes_back_as_the_plan_tree(rome):
    """EXPLAIN is one text column and is drawn exactly as PostgreSQL indented
    it: no ljust, no strip. The plan tree is the artifact."""
    db, session = rome
    view = gui.View()
    world = gui.read_world(db, session, view, gui.DEFAULT_SIZE)
    typing(db, session, view, world,
           "EXPLAIN SELECT * FROM tile WHERE x = 8 AND y = 7")
    assert list(view.result[0]) == ["QUERY PLAN"]
    surface = pygame.Surface(gui.DEFAULT_SIZE)
    gui.draw(surface, world, session, view, {}, db)      # and it draws


def test_a_statement_that_returns_nothing_says_no_rows(rome):
    db, session = rome
    view = gui.View()
    world = gui.read_world(db, session, view, gui.DEFAULT_SIZE)
    typing(db, session, view, world, "UPDATE civ SET gold = 99 WHERE civ_id = 1")
    assert view.result == []
    surface = pygame.Surface(gui.DEFAULT_SIZE)
    gui.draw(surface, world, session, view, {}, db)
    assert core.snapshot(db, session)["civ"]["gold"] == 99


def test_the_prompt_still_works_with_no_world_at_all(dsn):
    """Typing `n` is the way out of an empty database, so the prompt cannot be
    behind the world existing."""
    db = dbapi.DB(dsn=dsn)
    db.rows("TRUNCATE world, civ CASCADE")
    session, view = core.Session(), gui.View()
    world = gui.read_world(db, session, view, gui.DEFAULT_SIZE)
    assert world["snap"] is None

    surface = pygame.Surface(gui.DEFAULT_SIZE)
    gui.draw(surface, world, session, view, {}, db)      # draws the prompt
    typing(db, session, view, world, "n 20 12 1 7")
    assert view.trouble == ""
    world = gui.read_world(db, session, view, gui.DEFAULT_SIZE)
    assert world["snap"]["world"]["width"] == 20
