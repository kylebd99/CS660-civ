# civ — a strategy game that lives in PostgreSQL

A Civ-like whose entire state *and rules* are a PostgreSQL database. Built as
the running example for **GRS CS 660, Graduate Introduction to Database
Systems** at Boston University.

There are two clients. Neither contains any game logic, and both print every
statement they send. If you want to know how the game works, read `sql/` — that
is the whole game.

```
$ make up && make reset && make play

turn 4  Rome  6g  9 science  researching agriculture
  @ Roma       pop 1  +5F 3P 2G  (stored 9 food)

(-2,11) . ~ ~ ~ ~ ~ ~ . . . , , , , , , . . . . . . . . , , # # # (34,11)
    , . ~ ~ ~ ~ ~ ~ ~ . . , , , , , , , . . . ~ ~ ~ ~ . . , , # n n ^ ^
    , . ~ ~ ~ ~ ~ ~ . . , , , # # # , , . . ~ ~ ~ ~ ~ ~ . . , # n n ^ ^
    , . ~ ~ ~ ~ ~ ~ . . , , # # # # , @ . . ~ ~ ~ ~ ~ ~ ~ . , # n ^ ^ ^
    , . ~ ~ ~ ~ ~ ~ . . , # # # # # w , . . ~ ~ ~ ~ ~ ~ ~ . , # n n ^ ^
    , . . ~ ~ ~ ~ . . , , # # # # # , , . ~ ~ ~ ~ ~ ~ ~ ~ . , , # n ^ ^
    , . . . ~ ~ . . . , , # # # # # , , . ~ ~ ~ ~ ~ ~ ~ ~ . . , # n ^ ^
    , , . . . . . . , , , # # # # , , . . ~ ~ ~ ~ ~ ~ ~ ~ . . , # # n n
(-2,3), , , , , , , , , , , , , , , , . . ~ ~ ~ ~ ~ ~ ~ ~ . . , , #(34,3)

  2:warrior(16,7) 20hp 2mp 1ap
  SELECT set_research('agriculture')
  SELECT end_turn() AS turn
  SELECT end_turn() AS turn
```

`make gui` opens the same game in a window instead, with the statement log as a
column beside the map rather than three lines under it. Two windows on one
database are two players — `python3 client/gui.py --half` opens at half width,
which is how two of them fit side by side on one projector.

## Running it

```sh
make deps      # pip install psycopg and pygame-ce
make up        # start postgres in docker
make reset     # load sql/*.sql and deal a world
make play      # attach the terminal client
make gui       # or the windowed one
make sql       # a psql prompt on the same live game

make dev-deps  # pip install pytest and pgserver
make test      # needs neither docker nor a running postgres
```

`make reset` is destructive and instant — it is how you start over.

`make test` starts its own PostgreSQL from a pip wheel and drives the window
against SDL's dummy driver, so it needs no Docker, no system PostgreSQL and no
display, and it leaves whatever world `make reset` last dealt alone.

## What is where

```
sql/01_schema.sql   the tables. every fact the game knows
sql/02_grid.sql     square-grid geometry and the terrain function
sql/03_seed.sql     the rulebook: terrain yields, unit stats, the tech tree
sql/04_views.sql    everything derived: the economy, the tech frontier, movement
sql/05_rules.sql    everything you can do: new_game, found_city, move_unit,
                    buy_unit, attack, set_research, end_turn

client/db.py        the connection, and every statement it runs
client/queries.py   every SQL string the clients send. imports nothing
client/game.py      what both clients need: identity, the view, typed actions
client/render.py    rows to characters
client/civ.py       the terminal client
client/gui.py       the windowed client
client/palette.py   ANSI colour numbers to RGB
```

`client/game.py` is the boundary. The rule that keeps it small is in its
docstring: **never duplicate a query or a rule; duplicate parsing and
formatting freely.** A test parses it with `ast` and fails if it ever imports a
front-end.

About 800 lines of SQL and 3,000 of Python, of which the two clients are most
of it. The SQL is meant to be read in an hour.

## Commands

The same letters in both clients — in the window they are typed into the prompt
bar, which also takes raw SQL.

| | |
|---|---|
| `n [w] [h] [civs] [seed]` | new game |
| `m <unit> <x> <y>` | move a unit |
| `a <unit> <x> <y>` | attack an adjacent enemy |
| `b [type x y]` | list unit prices, or buy one |
| `c <unit> <name>` | found a city with a settler |
| `t [tech]` | list, or choose, research |
| `s <unit>` | highlight where a unit can go |
| `e` | end the turn |
| `p [civ]` | list civs, or play as one |
| `v [x y]` | centre the view |
| `y [food\|prod\|gold]` | colour the map by yield |
| `: <sql>` | **run any SQL against the live game** |
| `?` `q` | help, quit |

In the terminal, click a unit and then a square to move it, and shift+arrows
pan. In the window, click and click; the arrows step the selected unit one tile
at a time, `shift+B` and `shift+T` open the shop and the tech list, space ends
the turn, and hovering the map shows the statement a click would send before
you send it.

## The parts worth reading

**The economy is one query.** `city_yield` in `sql/04_views.sql` is an aggregate
over the tiles each city works. There is no other source of food, production or
gold anywhere in the codebase. A city works its best *population* tiles, which
is a window function ranking tiles within each city.

**Techs change the yields, relationally.** `gathering_rate` joins each civ's
known techs against `terrain_bonus`, so researching Mining raises what hills
produce for that civ and nobody else. Nothing is written when a tech lands; the
numbers were always derived.

**The tech tree is relational division.** "Which techs can I research now?"
means *ones I don't know, all of whose prerequisites I do know* — universal
quantification, encoded as the doubly nested `NOT EXISTS` in `available_tech`.

**A turn is six statements.** `end_turn()` in `sql/05_rules.sql`. None of them
mentions a particular city, unit or civ; each operates on every row at once.
Read it next to the loop you would have written instead.

**Movement is a recursive walk.** `reachable()` expands over adjacency
accumulating terrain costs. It is deliberately the naive formulation — it
re-expands tiles reached by several paths and takes the minimum at the end. Its
`cost` column is drawn in the corner of each highlighted square.

**Who you are is a session setting.** The rules call `current_civ()`, which
reads `app.civ_id`, rather than taking a civ as an argument — for the same
reason a web application reads the session cookie instead of a `user_id` form
field. Two clients on one database are two players, and neither can move the
other's units even by typing SQL.

**The database refuses illegal moves.** `unit_one_per_tile` is a unique index,
not a check in the client. Try walking one unit onto another and read the
error — both clients show it, and the window shows the SQLSTATE with it.

## Demos

```sh
make demo-economy    # the yield query, the tiles behind it, and its plan
make demo-scale      # 280k tiles: index lookup vs sequential scan
```

`demo-scale` deals a 700×400 world and runs the lookup the renderer does on
every frame, with and without the `(x, y)` index. On the machine this was
written on: **0.039 ms indexed, 19 ms unindexed.**

That difference is not hypothetical. The clients were once sluggish on large
maps, and the cause was neither Python nor the renderer: after a bulk insert
`pg_class.reltuples` was still `-1`, so the planner sequentially scanned 280,000
tiles to find seven units. `new_game` now ends with `ANALYZE`, and a frame went
from 51.9 ms to about 6 ms.

## What is not built yet

- **Fog of war.** A `visibility` table and a `visible_world` view keyed on a
  session variable, so each player queries the same tables and sees a different
  map. Both clients currently show rival units where they stand.
- **The isolation-level demos.** Two clients can already play at once, and the
  window shows a serialization failure with its `40001` rather than retrying it
  quietly. What is missing is the scripted pair of transactions: two units onto
  one tile, and write skew leaving a city undefended.
- **Worked-tile borders.** `UNIQUE (tile_id)` on `city_tile` stops two cities
  harvesting the same tile, which is worth *seeing* on the map. It needs a view
  that neither client has yet.
- **Production queues.** Cities grow but cannot build anything.

## Licence

Course material. Do as you like with it.
