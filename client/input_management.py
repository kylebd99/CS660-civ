"""Everything between the keyboard and the game.

civ.py never touches termios, escape sequences or mouse reports. It asks this
module for keys and hands them back to be turned into edits to the input line.

(The name shadows the built-in `input()` inside modules that import it. Nothing
here uses that built-in -- reading a line at a time is exactly what this module
exists to avoid.)
"""

import os
import re
import select
import sys
import termios
import tty
from contextlib import contextmanager


# How often the screen redraws while waiting for you to type, so another
# player's moves show up without you having to press a key.
REFRESH_SECONDS = 0.1


# Ask the terminal to report clicks, in SGR mode so columns past 223 still fit.
MOUSE_ON = "\x1b[?1000;1006h"


MOUSE_OFF = "\x1b[?1000;1006l"


# Shift+arrow pans the view. A hex is two columns wide, so a horizontal step of
# two keeps whole hexes under the cursor.
PAN = {"\x1b[1;2A": (0, 2), "\x1b[1;2B": (0, -2),
       "\x1b[1;2C": (4, 0), "\x1b[1;2D": (-4, 0)}


MOUSE_REPORT = re.compile(r"\x1b\[<(\d+);(\d+);(\d+)([Mm])")


# Any CSI sequence: plain arrows are "\x1b[A", shifted ones "\x1b[1;2A",
# SGR mouse reports "\x1b[<0;12;9M". Matching them whole keeps their letters
# out of the prompt.
CSI = re.compile(r"\x1b\[[0-9;<]*[A-Za-z~]")


def parse_keys(data):
    """Split a chunk of terminal input into keys.

    Ordinary characters come back one at a time; escape sequences -- arrows and
    mouse reports -- come back whole, because iterating them character by
    character would type "[A" into the prompt.
    """
    i = 0
    while i < len(data):
        if data[i] == "\x1b":
            click = MOUSE_REPORT.match(data, i)
            if click:
                yield click.group(0)
                i = click.end()
                continue
            csi = CSI.match(data, i)
            if csi:
                yield csi.group(0)
                i = csi.end()
                continue
            i += 1                  # a bare escape, or a sequence cut in half
            continue
        yield data[i]
        i += 1


def edit_line(line, key, history):
    """Apply one keystroke to the input line.

    `line` is a dict of {text, at, draft}. Returns the finished command when
    Enter is pressed, otherwise None. Up and Down walk the history the way a
    shell does: whatever you were part-way through typing is kept as `draft`
    and comes back when you walk past the newest entry.
    """
    if key in ("\r", "\n"):
        done, line["text"], line["at"] = line["text"].strip(), "", None
        return done
    if key == "\x1b[A":
        if history:
            if line["at"] is None:
                line["draft"], line["at"] = line["text"], len(history)
            line["at"] = max(0, line["at"] - 1)
            line["text"] = history[line["at"]]
    elif key == "\x1b[B":
        if line["at"] is not None:
            line["at"] += 1
            if line["at"] >= len(history):
                line["at"], line["text"] = None, line["draft"]
            else:
                line["text"] = history[line["at"]]
    elif key in ("\x7f", "\b"):
        line["text"] = line["text"][:-1]
    elif key.isprintable():
        line["text"] += key
    return None


@contextmanager
def mouse_reporting():
    """Ask the terminal for click reports, and always stop asking. Left behind,
    the escape codes make an ordinary shell spew gibberish when you click."""
    print(MOUSE_ON, end="", flush=True)
    try:
        yield
    finally:
        print(MOUSE_OFF, end="", flush=True)


@contextmanager
def quiet_interrupt():
    """Let Ctrl-C end the game rather than spraying a traceback. cbreak keeps
    ISIG on, so Ctrl-C arrives as a signal, not as a byte we could read."""
    try:
        yield
    except KeyboardInterrupt:
        pass


@contextmanager
def raw_terminal():
    """Deliver keystrokes as they are typed instead of a line at a time.

    cbreak rather than full raw mode, so Ctrl-C still interrupts. The original
    settings are always restored, including on a crash.
    """
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        # cbreak leaves echo on, which would print your keystrokes wherever the
        # cursor happens to be sitting mid-redraw. We echo the typed line
        # ourselves as part of the frame, so turn the terminal's own echo off.
        attrs = termios.tcgetattr(fd)
        attrs[3] &= ~termios.ECHO
        termios.tcsetattr(fd, termios.TCSADRAIN, attrs)
        yield
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)


@contextmanager
def terminal():
    """Put the terminal into the state the game needs, and always undo it."""
    with raw_terminal(), mouse_reporting(), quiet_interrupt():
        yield


def keys(timeout=REFRESH_SECONDS):
    """Every key waiting right now, or nothing if `timeout` passes first.

    Reads the descriptor rather than sys.stdin. A buffered reader would pull
    several bytes in and hand back one, and select() polls the descriptor, not
    that buffer -- so the rest would sit there unseen until the next keystroke.
    Taking everything available also makes pasting a command work.
    """
    fd = sys.stdin.fileno()
    if not select.select([fd], [], [], timeout)[0]:
        return
    yield from parse_keys(os.read(fd, 1024).decode(errors="ignore"))


def mouse_press(key):
    """Where a left-button press landed, as 1-based (column, row).

    None for anything else: releases, other buttons, and every key that is not
    a mouse report at all.
    """
    report = MOUSE_REPORT.fullmatch(key)
    if not report:
        return None
    button, column, row, kind = report.groups()
    if kind != "M" or button != "0":
        return None
    return int(column), int(row)
