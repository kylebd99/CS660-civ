"""What both front-ends need, and nothing either of them needs alone.

The rules are in sql/. The statements are in queries.py. This is the thin layer
between them: who you are playing, what you are looking at, and one typed
function per thing you can do.

The rule that keeps this file small:

    Never duplicate a query or a rule. Duplicate parsing and formatting freely.

`int(args[0])` appearing in two front-ends costs thirty lines and reads fine. A
second copy of a query, or a second answer to "which tiles are in view", does
not -- that is how a shared core turns into a widget layer.

Nothing here catches psycopg.Error. The database refusing an illegal move is
the feature, and each front-end decides how to show it.
"""

from dataclasses import dataclass, field

import queries as q


@dataclass
class Session:
    """What this client is looking at. The game itself is in Postgres.

    Every field is a fact about *this* window rather than about the world,
    which is why two windows can hold two of these against one database. A
    front-end's own state -- a half-typed line, a scroll position -- does not
    belong here; the two containers being different types is the boundary.
    """

    civ_id: int | None = None                  # mirrors app.civ_id on the wire
    view: tuple[int, int] | None = None        # None = follow my own pieces
    overlay: str | None = None                 # 'food' | 'production' | 'gold'
    highlight: set = field(default_factory=set)
    log: list = field(default_factory=list)    # every statement sent


# ------------------------------------------------------------------ identity

def use_civ(db, session, civ_id):
    """Point this session at a civ, on both sides of the wire.

    The database half is what matters: the rules functions read current_civ()
    rather than trusting a civ id passed in with the request, so the server
    decides what you may touch. Telling the client alone would let any typed
    query act for anyone.
    """
    session.civ_id = civ_id
    session.view = None            # look at the new player's own territory
    db.call(q.SET_SESSION_CIV, (str(civ_id),))


def active_civ(db, session):
    """Which civ this client is playing, defaulting to the first one.

    Two windows on the same database are two players.
    """
    if session.civ_id is None:
        row = db.one(q.FIRST_CIV)
        if row and row["civ_id"] is not None:
            use_civ(db, session, row["civ_id"])
    return session.civ_id


def all_civs(db):
    return db.rows(q.ALL_CIVS)


# ---------------------------------------------------------------------- view

def view_centre(db, session):
    """Middle of the view, as a tile. Unset, it follows your pieces."""
    if session.view is None:
        home = db.one(q.VIEW_HOME, (active_civ(db, session),) * 2)
        session.view = (home["x"], home["y"]) if home else (0, 0)
    return session.view


def look_at(session, x=None, y=None):
    """Centre on a tile, or with no arguments go back to following pieces."""
    session.view = None if x is None else (x, y)


def pan(db, session, dx, dy):
    cx, cy = view_centre(db, session)
    session.view = (cx + dx, cy + dy)


def window_for(centre, wide, high):
    """The tile rectangle a viewport `wide` x `high` tiles covers.

    The only geometry the two front-ends share. What a tile *is* on screen is
    each front-end's business -- character cells one side, pixels the other --
    but how many fit and which ones they are is the same question, and the same
    four numbers map_window() wants.
    """
    cx, cy = centre
    return (cx - wide // 2, cx + wide // 2, cy - high // 2, cy + high // 2)


# ---------------------------------------------------------------------- reads

def snapshot(db, session):
    """Everything on screen except the tiles. None when there is no world yet.

    The tiles need a window, and a window needs a size that the caller works
    out for itself -- the terminal's depends on how many cities it has to list.
    So this is two calls rather than one, which is honest about the dependency
    and avoids reading the cities twice.
    """
    world = db.one(q.WORLD)
    if world is None:
        return None

    me = active_civ(db, session)
    units, cities = db.rows(q.UNITS), db.rows(q.CITIES)
    mine = lambda rows: [row for row in rows if row["civ_id"] == me]

    # units and cities are everyone's, because you can see them standing
    # there; my_* is your dossier. Hiding rival pieces is fog of war, which is
    # a different question and is not built.
    return {"world": world,
            "civ": db.one(q.CIV, (me,)),
            "civ_id": me,
            "units": units, "cities": cities,
            "my_units": mine(units), "my_cities": mine(cities)}


def tiles_in(db, session, window):
    """The window's tiles, and the scale to read them against.

    Returns (rows, highest). With no overlay the rows are terrain, carrying a
    glyph and a colour, and highest is None. With one, they carry a value and
    an intensity, and highest is the best value in view -- each front-end maps
    intensity into its own colour space.
    """
    if session.overlay:
        return overlay_cells(db.rows(q.YIELDS, (session.civ_id, *window)),
                             session.overlay)
    return db.rows(q.TILES, window), None


def reachable(db, unit):
    return {(row["x"], row["y"]) for row in db.rows(q.REACHABLE, (unit,))}


def unit_shop(db, civ_id):
    return db.rows(q.UNIT_SHOP, (civ_id,))


def available_techs(db, civ_id):
    return db.rows(q.AVAILABLE_TECH, (civ_id,))


def my_unit_at(db, x, y, civ_id):
    row = db.one(q.MY_UNIT_AT, (x, y, civ_id))
    return row["unit_id"] if row else None


# -------------------------------------------------------------------- actions
# One per rule function in sql/05_rules.sql. `session` appears exactly when the
# call reads or writes client-side state, which is why move() has no civ
# argument: move_unit() reads current_civ(), so the server decides whose unit
# it is. Passing an id here would be a suggestion, not a permission.

def move(db, unit, x, y):
    """Walk a unit. Returns the movement it spent."""
    return db.call(q.MOVE_UNIT, (unit, x, y))["cost"]


def attack(db, unit, x, y):
    """Strike an adjacent enemy. Returns the outcome as Postgres wrote it."""
    return db.call(q.ATTACK, (unit, x, y))["outcome"]


def buy(db, kind, x, y):
    return db.call(q.BUY_UNIT, (kind, x, y))["outcome"]


def found_city(db, unit, name="New City"):
    return db.call(q.FOUND_CITY, (unit, name))["city_id"]


def set_research(db, tech):
    db.call(q.SET_RESEARCH, (tech,))


def end_turn(db):
    """Advances the world for every civ, which is what end_turn() does."""
    return db.call(q.END_TURN)["turn"]


def new_game(db, session, width=30, height=16, civs=1, seed=42):
    db.call(q.NEW_GAME, (width, height, seed, civs))
    session.civ_id = None          # whoever you were does not exist any more
    session.view = None


def run_sql(db, session, sql):
    """Send arbitrary SQL. This is the whole pitch: the world is a database, so
    anything you can express you can do. Logged, because everything is."""
    rows = db.rows(sql)
    session.log.append(sql)
    return rows


# --------------------------------------------------------------- highlighting

def highlight_reachable(db, session, unit):
    """Light up where a unit could walk.

    Logged by hand: reachable() is a read, so db.call would not echo it, but
    a recursive CTE is worth watching go past.
    """
    session.highlight = reachable(db, unit)
    session.log.append(db.rendered(q.REACHABLE, (unit,)))


def clear_highlight(session):
    session.highlight = set()


# ------------------------------------------------------------------- overlays

# What the front-ends will accept, and which column of yield_window each one
# colours. Production is what end_turn turns into science.
OVERLAYS = {"food": "food", "f": "food",
            "production": "production", "prod": "production", "p": "production",
            "gold": "gold", "g": "gold"}


def overlay_cells(rows, field_name):
    """Yield rows as cells carrying a value and an intensity.

    intensity runs 0.0 for nothing at all to 1.0 for the best in view, and each
    front-end maps it into its own colour space. Anchored at zero rather than at
    the lowest tile on screen, so a barren tile stays cold instead of turning
    warm just because everything around it is barren too.
    """
    highest = max((row[field_name] for row in rows), default=0)
    cells = [{"x": row["x"], "y": row["y"], "value": row[field_name],
              "intensity": 0.0 if highest <= 0 else row[field_name] / (highest + 1)}
             for row in rows]
    return cells, highest
