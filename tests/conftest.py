"""Fixtures: a real PostgreSQL, loaded with the real schema.

`pgserver` ships a PostgreSQL binary as a pip wheel and runs it rootless in a
temporary directory, so the tests need neither Docker nor a system install --
which is why `make test` does not depend on `make up`.

Nothing here stubs the database. The whole point of the project is that the
rules live in SQL, so a test against a fake would be testing nothing.
"""

import os
import pathlib
import re
import subprocess
import sys

import pgserver
import psycopg
import pytest
from psycopg.rows import dict_row

ROOT = pathlib.Path(__file__).resolve().parent.parent
CLIENT = ROOT / "client"
SQL = ROOT / "sql"

# client/ is not a package: `python3 client/civ.py` works because that puts the
# directory on sys.path, and the flat imports there rely on it. Tests are not
# run from inside client/, so they have to arrange the same thing.
sys.path.insert(0, str(CLIENT))

# Matches every escape sequence the terminal client emits, so a frame can be
# compared as plain text.
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


@pytest.fixture(scope="session")
def dsn(tmp_path_factory):
    """A PostgreSQL with sql/*.sql loaded, shared by the whole session."""
    server = pgserver.get_server(tmp_path_factory.mktemp("pgdata"))
    uri = server.get_uri()

    # Loading is idempotent: 01_schema.sql opens with DROP SCHEMA ... CASCADE.
    # It also ends with ALTER DATABASE ... SET, which only takes effect on
    # connections opened afterwards -- hence a throwaway connection here and
    # fresh ones in every fixture below.
    with psycopg.connect(uri, autocommit=True) as conn:
        for path in sorted(SQL.glob("*.sql")):
            conn.execute(path.read_text())
    return uri


@pytest.fixture
def db(dsn):
    """A connection that has picked up the database-level settings."""
    with psycopg.connect(dsn, autocommit=True, row_factory=dict_row) as conn:
        yield conn


@pytest.fixture
def game(db):
    """A fresh two-civ world. Two civs because two players is a requirement,
    and a one-civ world cannot catch an ownership bug."""
    db.execute("SELECT new_game(30, 16, 42, 2)")
    return db


@pytest.fixture
def play(dsn):
    """Drive the terminal client with a piped script.

    civ.py takes its scripted path when stdin is not a tty, and that path emits
    one frame per command -- so no pty is needed to capture what the client
    drew. Returns the frames as plain text, escapes stripped.
    """
    def run(script, columns=80, lines=24, civ=None):
        env = {**os.environ, "CIV_DSN": dsn,
               "COLUMNS": str(columns), "LINES": str(lines)}
        if civ is not None:
            script = [f"p {civ}", *script]
        done = subprocess.run(
            [sys.executable, "civ.py"], cwd=CLIENT,
            input="\n".join([*script, "q"]) + "\n",
            capture_output=True, text=True, timeout=180, env=env)
        assert done.returncode == 0, done.stderr
        assert "Traceback" not in done.stderr, done.stderr
        # Each redraw homes the cursor, so that is the frame delimiter.
        return [ANSI.sub("", frame) for frame in done.stdout.split("\x1b[H")[1:]]
    return run
