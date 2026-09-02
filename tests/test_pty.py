"""The one thing that needs a real terminal: that we hand it back.

Everything else about the terminal client is tested through its scripted path,
which needs no pty. But `im.terminal()` puts the tty into cbreak with echo off,
and if it ever failed to restore that, the shell you launched the game from
would be left mute -- a bad five seconds in front of a lecture hall, and
invisible to every other test here.
"""

import os
import pathlib
import pty
import subprocess
import sys
import termios
import threading
import time

CLIENT = pathlib.Path(__file__).resolve().parent.parent / "client"


def test_terminal_settings_are_restored(dsn, db):
    db.execute("SELECT new_game(20, 10, 42, 1)")

    master, slave = pty.openpty()
    # Popen dups the slave and closes its copy on exit, so keep our own handle
    # to inspect the settings afterwards.
    ours = os.dup(slave)
    before = termios.tcgetattr(ours)

    proc = subprocess.Popen(
        [sys.executable, "civ.py"], cwd=CLIENT,
        stdin=slave, stdout=slave, stderr=slave,
        close_fds=True, start_new_session=True,
        env={**os.environ, "CIV_DSN": dsn, "TERM": "xterm",
             "COLUMNS": "80", "LINES": "24"})
    os.close(slave)

    # Drain continuously: a full pty buffer would block the client mid-frame.
    draining = True

    def drain():
        while draining:
            try:
                os.read(master, 65536)
            except OSError:
                return

    threading.Thread(target=drain, daemon=True).start()

    try:
        time.sleep(1.5)
        assert proc.poll() is None, "client exited early"

        playing = termios.tcgetattr(ours)
        assert not playing[3] & termios.ECHO, "echo should be off while playing"
        assert not playing[3] & termios.ICANON, "cbreak should be on while playing"

        os.write(master, b"q\r")
        proc.wait(timeout=20)
        assert proc.returncode == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)
        draining = False

    assert termios.tcgetattr(ours) == before, "the tty was not put back"
    os.close(ours)
    os.close(master)
