"""The only module that talks to PostgreSQL.

Two responsibilities: hold the connection, and hand back every statement it
runs so the UI can show it. The echo is not debugging output -- watching the
SQL is the entire point of the exercise.

Three tiers, deliberately:

    rows()      a read behind a redraw. Silent, because the UI reads
                constantly and echoing that would drown out everything else.
    watched()   a read worth seeing: one you typed, or a recursive walk.
    call()      a write. Always echoed.

What comes back with each echoed statement is a Sent: the statement as it went
out, how long it took, how many rows it returned, and -- if PostgreSQL refused
it -- the SQLSTATE and the message. A refusal is the most interesting thing
this file can report, so it is reported rather than swallowed, and then
re-raised for whoever asked.
"""

import os
import time
from dataclasses import dataclass

import psycopg
from psycopg.rows import dict_row

DEFAULT_DSN = "postgresql://civ:civ@localhost:5432/civ"


@dataclass
class Sent:
    """One statement and what came of it.

    Stringifies to the statement itself, so anything that only wants to print
    the SQL can, and a front-end that wants the timing can reach for it.
    """

    sql: str
    ms: float = 0.0
    rows: int | None = None            # None when the statement was refused
    error: str | None = None           # SQLSTATE and the first line of it

    def __str__(self):
        return self.sql

    @property
    def outcome(self):
        """The annotation a log line carries: what came back, and how long it
        took to come back."""
        if self.error:
            return self.error
        count = "" if self.rows is None else f"-> {self.rows}   "
        return f"{count}{self.ms:.1f} ms"


class DB:
    def __init__(self, dsn=None, echo=None):
        self.conn = psycopg.connect(
            dsn or os.environ.get("CIV_DSN", DEFAULT_DSN),
            autocommit=True,
            row_factory=dict_row,
        )
        self.conn.execute("SET search_path TO game, public")
        self.echo = echo or (lambda sent: None)
        self.quiet = 0                 # silent reads, for the log's rollup line

    def rows(self, sql, params=()):
        """Read. Returns a list of dicts, and is not echoed -- the UI reads
        constantly to redraw, and echoing that would drown out the interesting
        statements. They are counted, so a front-end can still say how many
        there have been."""
        self.quiet += 1
        return self.run(sql, params)

    def one(self, sql, params=()):
        rows = self.rows(sql, params)
        return rows[0] if rows else None

    def watched(self, sql, params=()):
        """A read worth showing: one the player typed, or a recursive walk that
        is interesting to watch go past."""
        return self.run(sql, params, echo=True)

    def call(self, sql, params=()):
        """Write. Echoed, because these are the statements that change the
        world and the player should see exactly what was sent."""
        rows = self.run(sql, params, echo=True)
        return rows[0] if rows else None

    def run(self, sql, params=(), echo=False):
        """Execute one statement, timing it, and report it if asked."""
        started = time.perf_counter()
        try:
            with self.conn.cursor() as cur:
                cur.execute(sql, params)
                rows = cur.fetchall() if cur.description else []
        except psycopg.Error as exc:
            if echo:
                self.echo(Sent(self.rendered(sql, params), self.since(started),
                               None, f"{exc.sqlstate} "
                                     f"{str(exc).strip().splitlines()[0]}"))
            raise
        if echo:
            self.echo(Sent(self.rendered(sql, params), self.since(started),
                           len(rows)))
        return rows

    @staticmethod
    def since(started):
        return (time.perf_counter() - started) * 1000

    def in_transaction(self):
        """Whether this connection is inside a transaction.

        The connection is opened with autocommit on, so this is only ever true
        because someone typed BEGIN. Worth showing: without it a typed BEGIN
        silently changes the connection's state with nothing on screen, and for
        the concurrency lectures that is a defect in the teaching.
        """
        return self.conn.info.transaction_status != psycopg.pq.TransactionStatus.IDLE

    def rendered(self, sql, params=()):
        """The statement with its parameters filled in, for display only.
        psycopg still sends the query and the parameters separately."""
        return psycopg.ClientCursor(self.conn).mogrify(sql, params)
