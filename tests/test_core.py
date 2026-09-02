"""The shared core, tested with no terminal anywhere in sight.

That is the point of the file. Everything here drives `client/game.py` directly
against a real PostgreSQL, so these tests describe what *any* front-end can
rely on -- which is what makes it safe to write a second one.

The module is imported as `core` because `game` is already the name of the
fixture that deals a world, and shadowing it inside every test would be worse
than one alias here.
"""

import ast
import pathlib

import psycopg
import pytest

import db as dbapi
import game as core
import queries as q

CLIENT = pathlib.Path(dbapi.__file__).parent


@pytest.fixture
def core_db(dsn):
    """The client's own DB wrapper, logging into a list we can inspect."""
    log = []
    return dbapi.DB(dsn=dsn, echo=log.append), log


@pytest.fixture
def world(game, dsn):
    """A session and connection on the two-civ world `game` just dealt, having
    not yet said who it is playing.

    A second connection rather than reusing the fixture's: that is what a real
    client is, and under autocommit it sees the same rows.
    """
    session = core.Session()
    return dbapi.DB(dsn=dsn, echo=session.log.append), session


@pytest.fixture
def rome(world):
    """The same, playing civ 1.

    Separate from `world` because "has not chosen a civ yet" is a real state
    with its own behaviour -- the server refuses to act at all -- and a fixture
    that quietly picked one would hide it.
    """
    db, session = world
    core.use_civ(db, session, 1)
    return db, session


# The world new_game(30, 16, 42, 2) deals, which the tests below name directly.
# Reproducible since Phase 0, so writing coordinates down is safe.
SETTLER, WARRIOR = 1, 2                            # civ 1, at (8,7) and (7,6)
THEIR_SETTLER, THEIR_WARRIOR = 3, 4                # civ 2, at (20,10), (19,9)


# ------------------------------------------------------- the dependency rule
# One test, permanent guard. The core is only worth extracting if it cannot
# reach back into a front-end, and an import is how that would start.

def imported_names(filename):
    """Top-level module names imported by a file, however it imports them."""
    tree = ast.parse((CLIENT / filename).read_text())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split(".")[0])
    return names


def test_core_imports_nothing_from_a_front_end():
    assert imported_names("game.py") <= {"queries", "db", "psycopg", "dataclasses"}


def test_queries_imports_nothing_at_all():
    """It is a file of strings. Anything it imported would be a rule leaking
    out of SQL and into Python."""
    assert imported_names("queries.py") == set()


@pytest.mark.parametrize("front_end", ["civ.py", "gui.py"])
def test_front_ends_do_not_import_each_other(front_end):
    if not (CLIENT / front_end).exists():
        pytest.skip(f"{front_end} not written yet")
    others = {"civ", "gui"} - {front_end.removesuffix(".py")}
    assert not imported_names(front_end) & others


# -------------------------------------------------------------------- identity

def test_active_civ_defaults_to_the_first_civ(world):
    db, session = world
    assert core.active_civ(db, session) == 1


def test_active_civ_tells_the_server_too(world):
    """The rules read current_civ(), not an argument, so a client that only
    remembered its civ locally could act for anyone who typed SQL."""
    db, session = world
    core.active_civ(db, session)
    assert db.one("SELECT current_civ() AS me")["me"] == 1


def test_use_civ_switches_both_sides(world):
    db, session = world
    core.use_civ(db, session, 2)
    assert session.civ_id == 2
    assert db.one("SELECT current_civ() AS me")["me"] == 2


def test_all_civs_lists_everyone(world):
    db, _ = world
    assert [c["civ_id"] for c in core.all_civs(db)] == [1, 2]


def test_a_session_that_has_not_chosen_a_civ_cannot_act(world):
    """Identity is a session setting rather than an argument, so a connection
    that never announced itself can read the world but not touch it."""
    db, _ = world
    assert db.one(q.WORLD)["turn"] == 1
    with pytest.raises(psycopg.Error, match="no civ selected for this session"):
        core.move(db, WARRIOR, 8, 6)


# ------------------------------------------------------------------ the view
# `view is None` means "follow my own pieces", and three places assign it. The
# semantics are cheap to get wrong and invisible until the camera jumps.

def test_view_follows_your_units_until_you_look_somewhere(rome):
    db, session = rome
    assert session.view is None
    # Your lowest-numbered tile, which with no city is the warrior at (7,6) --
    # the settler is at (8,7) and loses the x, y tiebreak.
    assert core.view_centre(db, session) == (7, 6)


def test_view_prefers_a_city_to_a_unit(rome):
    """Cities rank above units in VIEW_HOME, so founding one moves the camera
    even though the units have not moved."""
    db, session = rome
    core.found_city(db, SETTLER, "Roma")               # on the settler's tile
    session.view = None
    assert core.view_centre(db, session) == (8, 7)


def test_look_at_pins_the_view_and_no_argument_releases_it(rome):
    db, session = rome
    core.look_at(session, 3, 4)
    assert core.view_centre(db, session) == (3, 4)
    core.look_at(session)
    assert session.view is None


def test_panning_starts_from_wherever_you_are_looking(rome):
    db, session = rome
    core.pan(db, session, 2, -3)                        # resolves (7,6) first
    assert session.view == (9, 3)


def test_new_game_forgets_who_you_were(rome):
    db, session = rome
    core.use_civ(db, session, 2)
    core.look_at(session, 3, 4)
    core.new_game(db, session, 20, 12, 1, 7)
    assert session.civ_id is None and session.view is None
    assert core.active_civ(db, session) == 1


def test_view_centre_of_an_empty_world_is_the_origin(core_db):
    db, _ = core_db
    db.rows("TRUNCATE world, civ CASCADE")
    assert core.view_centre(db, core.Session()) == (0, 0)


def test_window_for_is_centred_on_the_tile_you_asked_for():
    """Four numbers, and the only geometry the two front-ends share."""
    assert core.window_for((10, 5), 9, 5) == (6, 14, 3, 7)
    # Even sizes cannot be centred exactly, so they lean the same way both
    # axes: one extra tile right and up.
    assert core.window_for((0, 0), 4, 4) == (-2, 2, -2, 2)


# --------------------------------------------------------------------- reads

def test_no_world_no_snapshot(core_db):
    db, _ = core_db
    db.rows("TRUNCATE world, civ CASCADE")
    assert core.snapshot(db, core.Session()) is None


def test_snapshot_separates_everyones_pieces_from_yours(world):
    db, session = world
    snap = core.snapshot(db, session)
    assert snap["world"]["turn"] == 1
    assert snap["civ_id"] == 1
    # You can see rival units standing there -- fog of war is not built -- but
    # `my_units` is your dossier and drives the unit line and the shop.
    assert len(snap["units"]) == 4
    assert {u["unit_id"] for u in snap["my_units"]} == {1, 2}
    assert snap["cities"] == [] and snap["my_cities"] == []


def test_tiles_in_returns_terrain_and_no_scale(world):
    db, session = world
    rows, highest = core.tiles_in(db, session, core.window_for((8, 7), 5, 5))
    assert highest is None
    assert len(rows) == 25
    assert {"x", "y", "glyph", "colour"} <= set(rows[0])


def test_tiles_in_returns_values_and_a_scale_under_an_overlay(world):
    db, session = world
    core.active_civ(db, session)
    session.overlay = "food"
    cells, highest = core.tiles_in(db, session, core.window_for((8, 7), 5, 5))
    assert len(cells) == 25
    assert highest == max(cell["value"] for cell in cells)
    assert all(0.0 <= cell["intensity"] < 1.0 for cell in cells)


def test_reachable_says_what_each_tile_would_cost(rome):
    db, session = rome
    spots = core.reachable(db, WARRIOR)            # 2 mp
    assert spots[(7, 6)] == 0                      # where it already is
    assert spots[(8, 6)] == 1                      # one step onto grass
    assert max(spots.values()) == 2                # and no further than 2
    assert all(isinstance(spot, tuple) for spot in spots)


def test_the_shop_prices_everything_and_says_what_is_locked(rome):
    db, session = rome
    shop = {u["code"]: u for u in core.unit_shop(db, core.active_civ(db, session))}
    assert shop["warrior"]["unlocked"] is True
    # The knight is the whole reason unit_type has a required_tech column.
    assert shop["knight"]["unlocked"] is False
    assert shop["knight"]["required_tech"] == "bronze_working"


def test_my_unit_at_finds_only_your_own(rome):
    db, session = rome
    me = core.active_civ(db, session)
    assert core.my_unit_at(db, 8, 7, me) == 1
    assert core.my_unit_at(db, 20, 10, me) is None      # civ 2's settler
    assert core.my_unit_at(db, 0, 0, me) is None


# ------------------------------------------------------------------- actions
# Each returns what the SQL function returned, because the interesting part of
# a move is what it cost and the interesting part of a fight is what happened.

def test_move_returns_what_it_cost(rome):
    db, session = rome
    assert core.move(db, WARRIOR, 8, 6) == 1
    assert core.my_unit_at(db, 8, 6, 1) == WARRIOR


def test_founding_a_city_returns_its_id(rome):
    db, session = rome
    assert core.found_city(db, SETTLER, "Roma") == 1
    assert core.snapshot(db, session)["my_cities"][0]["name"] == "Roma"


def test_attack_returns_the_blow_by_blow(rome):
    db, session = rome
    # Stand civ 2's warrior next to ours. Marching it eleven tiles would test
    # move_unit, which has its own test; this test is about the sentence.
    db.rows("""UPDATE unit SET tile_id = (SELECT tile_id FROM tile
                                          WHERE x = 8 AND y = 6)
               WHERE unit_id = 4""")
    outcome = core.attack(db, 2, 8, 6)
    assert "warrior 2 hits warrior 4" in outcome
    assert "takes" in outcome


def test_buying_a_locked_unit_says_what_it_needs(rome):
    db, session = rome
    core.found_city(db, SETTLER, "Roma")
    with pytest.raises(psycopg.Error, match="a knight needs bronze_working"):
        core.buy(db, "knight", 8, 8)


def test_buying_an_affordable_unit_places_it(rome):
    db, session = rome
    core.found_city(db, SETTLER, "Roma")
    db.rows("UPDATE civ SET gold = 500 WHERE civ_id = 1")
    core.buy(db, "warrior", 8, 8)
    assert core.my_unit_at(db, 8, 8, 1) is not None


def test_research_has_to_be_available(rome):
    db, session = rome
    codes = {t["code"] for t in core.available_techs(db, 1)}
    assert "agriculture" in codes
    core.set_research(db, "agriculture")
    assert core.snapshot(db, session)["civ"]["researching"] == "agriculture"
    with pytest.raises(psycopg.Error, match="not available to you"):
        core.set_research(db, "currency")           # needs bronze_working first


def test_end_turn_advances_the_world(rome):
    db, session = rome
    assert core.end_turn(db) == 2
    assert core.snapshot(db, session)["world"]["turn"] == 2
    assert core.snapshot(db, session)["my_units"][0]["moves_left"] > 0


# ------------------------------------------------------------- two players
# Two Sessions on two connections is exactly what two windows are, and this is
# the test that says the server -- not the client -- decides whose unit is
# whose. Worth showing in the concurrency lecture.

def test_one_session_cannot_move_the_others_units(game, dsn):
    rome, egypt = core.Session(), core.Session()
    rome_db = dbapi.DB(dsn=dsn, echo=rome.log.append)
    egypt_db = dbapi.DB(dsn=dsn, echo=egypt.log.append)
    core.use_civ(rome_db, rome, 1)
    core.use_civ(egypt_db, egypt, 2)

    with pytest.raises(psycopg.Error, match="unit 3 is not yours"):
        core.move(rome_db, 3, 20, 11)               # civ 2's settler
    core.move(egypt_db, 3, 20, 11)                  # its owner may
    assert core.my_unit_at(egypt_db, 20, 11, 2) == 3


# ------------------------------------------------------------------ the log
# Every statement that changes the world is on screen. These tests are about
# that promise rather than about formatting, which each front-end owns.

def test_writes_are_logged_and_redraws_are_not(rome):
    db, session = rome
    session.log.clear()            # choosing a civ is itself a logged statement
    core.snapshot(db, session)
    core.tiles_in(db, session, core.window_for((8, 7), 5, 5))
    assert session.log == []                        # reads would drown the rest
    core.move(db, WARRIOR, 8, 6)
    assert session.log == ["SELECT move_unit(2, 8, 6) AS cost"]


def test_typed_sql_is_logged_even_though_it_is_a_read(rome):
    db, session = rome
    session.log.clear()
    rows = core.run_sql(db, session, "SELECT count(*) AS n FROM unit")
    assert rows[0]["n"] == 4
    assert session.log == ["SELECT count(*) AS n FROM unit"]


def test_asking_where_a_unit_can_go_shows_the_recursive_query(rome):
    db, session = rome
    core.highlight_reachable(db, session, WARRIOR)
    assert (7, 6) in session.highlight
    # Logged although it is only a read: the recursive walk is worth watching
    # go past, even though the WITH RECURSIVE itself lives in 04_views.sql.
    assert session.log[-1] == "SELECT x, y, cost FROM reachable(2)"
    core.clear_highlight(session)
    assert session.highlight == {}


# ---------------------------------------------------------------- overlays

def test_overlay_cells_are_anchored_at_zero():
    """A barren tile stays cold rather than turning warm because everything
    around it is barren too, which is why the scale starts at 0 and not at the
    lowest value in view."""
    rows = [{"x": 0, "y": 0, "food": 0}, {"x": 1, "y": 0, "food": 3}]
    cells, highest = core.overlay_cells(rows, "food")
    assert highest == 3
    assert cells[0]["intensity"] == 0.0
    assert 0.0 < cells[1]["intensity"] < 1.0        # never saturates


def test_overlay_cells_survive_a_world_with_nothing_in_it():
    cells, highest = core.overlay_cells([], "food")
    assert (cells, highest) == ([], 0)
    cells, highest = core.overlay_cells([{"x": 0, "y": 0, "gold": 0}], "gold")
    assert highest == 0 and cells[0]["intensity"] == 0.0


def test_every_overlay_name_names_a_column_of_yield_window(rome):
    """The aliases are a front-end convenience, but the values they map to have
    to be columns the view actually publishes."""
    db, session = rome
    columns = set(db.rows(q.YIELDS, (1, 0, 1, 0, 1))[0])
    assert set(core.OVERLAYS.values()) <= columns


# ------------------------------------------------ the terminal is a thin skin
# The last guarantee, and the cheapest: the same play through the terminal's
# dispatch and through the core leaves the database in the same state. If a
# cmd_* wrapper ever grows a rule of its own, this is what notices.

PLAY = [("c 1 Roma",       lambda db, s: core.found_city(db, SETTLER, "Roma")),
        ("t agriculture",  lambda db, s: core.set_research(db, "agriculture")),
        ("m 2 8 6",        lambda db, s: core.move(db, WARRIOR, 8, 6)),
        ("e",              lambda db, s: core.end_turn(db)),
        ("p 2",            lambda db, s: core.use_civ(db, s, 2)),
        ("c 3 Thebes",     lambda db, s: core.found_city(db, 3, "Thebes")),
        ("e",              lambda db, s: core.end_turn(db))]

STATE = ["SELECT unit_id, civ_id, type, tile_id, hp, moves_left FROM unit ORDER BY 1",
         "SELECT city_id, civ_id, name, population, food_store FROM city ORDER BY 1",
         "SELECT civ_id, gold, science, researching FROM civ ORDER BY 1"]


def replay(db, play_one):
    """Deal the same world again and play the same moves, one way or the other.

    Re-dealing is what makes the comparison possible, and it works because
    new_game is reproducible: terrain_at is a pure function of the seed, and
    both the settler and its escort now have an ordering tiebreak.
    """
    core.new_game(db, core.Session(), 30, 16, 2, 42)
    session = core.Session()
    core.active_civ(db, session)
    for step in PLAY:
        play_one(session, step)
    return [db.rows(sql) for sql in STATE]


def test_the_terminal_and_the_core_leave_the_same_database(core_db):
    import civ

    db, _ = core_db
    through_the_core = replay(db, lambda s, step: step[1](db, s))

    def typed(session, step):
        ui = {"note": "", "history": [], "running": True}
        civ.dispatch(db, session, ui, step[0])
        # dispatch swallows psycopg.Error into the note, so without this a
        # refused command would look like agreement between the two paths.
        assert ui["note"] == "", f"{step[0]}: {ui['note']}"

    through_the_terminal = replay(db, typed)
    assert through_the_terminal == through_the_core
