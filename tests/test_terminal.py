"""What the terminal client draws, and the log it writes while drawing it.

These exist to be the safety net for splitting civ.py into a shared core and a
terminal front-end. They deliberately assert on the rendered frames rather than
on the database, because the frames are what a refactor is most likely to
change by accident.

The output is reproducible: terrain_at() is a pure function of position and
seed, new_game() places civs deterministically, and combat has no random
element -- so a golden frame is a fair test rather than a flaky one.

Frames lag commands by one. run_scripted() draws, *then* dispatches, so the
effect of the Nth command appears in frame N+1.
"""

import os
import pathlib
import re

import pytest

GOLDEN = pathlib.Path(__file__).parent / "golden"


def map_block(frame):
    """The map lines of a frame.

    screen() returns [*status, "", *map, "", unit_line, ...], so the map is
    whatever sits between the first two blank lines.
    """
    lines = frame.split("\n")
    blank = [i for i, line in enumerate(lines) if not line.strip()]
    assert len(blank) >= 2, f"no map block in frame:\n{frame}"
    return lines[blank[0] + 1:blank[1]]


def check_golden(name, text):
    path = GOLDEN / name
    if os.environ.get("UPDATE_GOLDEN"):
        path.parent.mkdir(exist_ok=True)
        path.write_text(text)
        pytest.skip(f"wrote {path}")
    hint = f"    UPDATE_GOLDEN=1 python3 -m pytest tests/test_terminal.py"
    assert path.exists(), f"{path} is missing. Review the output, then:\n{hint}"
    assert text == path.read_text(), f"{name} changed. If intended:\n{hint}"


# ------------------------------------------------------------------ the frame

def test_status_line_names_the_active_civ(game, play):
    assert "turn 1  Rome  0g  0 science  researching nothing" in play([])[0]


def test_map_lines_all_have_the_same_width(game, play):
    """Ragged map lines meant the frame was being built by scanning every tile
    per row; uniform width is the property that replaced it."""
    block = map_block(play([])[0])
    assert len(block) >= 5
    assert len({len(line) for line in block}) == 1, [len(l) for l in block]


def test_corners_are_captioned_with_coordinates(game, play):
    """The window is wider than the 30x16 world, so captions name tiles outside
    it -- they say where you are looking, not what exists. Asserted as an
    invariant rather than fixed numbers, so the test survives a window-size
    change but still catches a broken caption or a flipped axis.
    """
    block = map_block(play([])[0])
    corners = [re.findall(r"\((-?\d+),(-?\d+)\)", line) for line in (block[0], block[-1])]
    (top_l, top_r), (bot_l, bot_r) = (
        (pair[0], pair[-1]) for pair in corners)

    assert top_l[0] == bot_l[0], "left captions disagree on x"
    assert top_r[0] == bot_r[0], "right captions disagree on x"
    assert int(top_l[1]) > int(bot_l[1]), "+y should run up the screen"
    assert int(top_r[0]) > int(top_l[0]), "+x should run right"


# --------------------------------------------------------------- your dossier

def test_unit_line_lists_only_your_own_units(game, play):
    """civ 2 owns units 3 and 4. The map shows everyone's pieces; the lines
    below it are your dossier and must not."""
    frames = play(["e"])
    assert any("1:settler" in f and "2:warrior" in f for f in frames)
    assert not any("3:settler" in f or "4:warrior" in f for f in frames)


def test_switching_civ_switches_the_dossier(game, play):
    frames = play([], civ=2)
    assert any("turn 1  Carthage" in f for f in frames)
    assert any("3:settler" in f for f in frames)
    assert not any("1:settler" in f for f in frames[1:])


# -------------------------------------------------------------------- actions

def test_founding_a_city_shows_in_the_status_and_the_log(game, play):
    frames = play(["c 1 Roma"])
    assert any("@ Roma" in f and "pop 1" in f for f in frames)
    assert any("SELECT found_city(1, 'Roma')" in f for f in frames)
    # The settler is consumed, so it leaves the dossier.
    assert "1:settler" not in frames[-1]


def test_every_write_reaches_the_log(game, play):
    """The log is the point of the project, so it is worth asserting that a
    statement cannot be issued without appearing."""
    frames = play(["c 1 Roma", "t agriculture", "e"])
    last = frames[-1]
    assert "SELECT found_city(1, 'Roma')" in last
    assert "SELECT set_research('agriculture')" in last
    assert "SELECT end_turn()" in last


def test_a_refused_move_is_reported(game, play):
    assert any("cannot reach (99, 99)" in f for f in play(["m 2 99 99"]))


def test_acting_on_another_civs_unit_is_refused(game, play):
    """current_civ() enforcement, seen from the front-end."""
    assert any("unit 3 is not yours" in f for f in play(["m 3 1 1"]))


# ------------------------------------------------------------------- overlays

def test_overlay_labels_the_status_line(game, play):
    """`showing food 0-N` is appended by rebuilding draw_status after the
    window is known -- an easy thing to drop in a refactor."""
    frames = play(["y food"])
    assert any("showing food 0-" in f for f in frames)


def test_overlay_draws_values_instead_of_terrain(game, play):
    frames = play(["y food"])
    overlaid = next(f for f in frames if "showing food" in f)
    block = map_block(overlaid)
    glyphs = {c for line in block for c in line} - set(" ()-,0123456789swk@")
    assert not glyphs, f"terrain glyphs left in an overlay: {glyphs}"


def test_bare_y_returns_to_terrain(game, play):
    frames = play(["y food", "y"])
    assert "showing" not in frames[-1]
    assert "~" in frames[-1] or "#" in frames[-1]


# --------------------------------------------------------------------- golden

def test_frames_match_golden(game, play):
    script = ["c 1 Roma", "t agriculture", "e", "e", "s 2", "y prod", "y", "p 2"]
    frames = play(script, columns=80, lines=24)
    check_golden("session.txt", "\n=== frame ===\n".join(frames))
