"""The only module that talks to PostgreSQL.

Two responsibilities: hold the connection, and hand back every statement it
runs so the UI can show it. The echo is not debugging output -- watching the
SQL is the entire point of the exercise.
"""

import os
import psycopg
from psycopg.rows import dict_row

DEFAULT_DSN = "postgresql://civ:civ@localhost:5432/civ"


class DB:
    def __init__(self, dsn=None, echo=None):
        self.conn = psycopg.connect(
            dsn or os.environ.get("CIV_DSN", DEFAULT_DSN),
            autocommit=True,
            row_factory=dict_row,
        )
        self.conn.execute("SET search_path TO game, public")
        self.echo = echo or (lambda sql: None)

    def rows(self, sql, params=()):
        """Read. Returns a list of dicts, and is not echoed -- the UI reads
        constantly to redraw, and echoing that would drown out the interesting
        statements."""
        with self.conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall() if cur.description else []

    def one(self, sql, params=()):
        rows = self.rows(sql, params)
        return rows[0] if rows else None

    def call(self, sql, params=()):
        """Write. Echoed, because these are the statements that change the
        world and the player should see exactly what was sent."""
        self.echo(self.rendered(sql, params))
        return self.one(sql, params)

    def rendered(self, sql, params=()):
        """The statement with its parameters filled in, for display only.
        psycopg still sends the query and the parameters separately."""
        return psycopg.ClientCursor(self.conn).mogrify(sql, params)
