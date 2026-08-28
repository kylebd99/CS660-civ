# civ — a strategy game that lives in PostgreSQL

A Civ-like whose entire state *and rules* are a PostgreSQL database. Built as
the running example for **GRS CS 660, Graduate Introduction to Database
Systems** at Boston University.

The client is a terminal that draws rows and calls functions. It contains no
game logic, and it prints every statement it sends. If you want to know how the
game works, read `sql/` — that is the whole game.

```
$ make up && make reset && make play

turn 9  Rome  0g  12 science  researching nothing
  @ Roma       pop 2  +6F 5P 0G  (stored 6 food)

     , # n ^ ^ ^
    , # # n ^ ^ ^
   . , # # n ^ ^ ^
  . . , # # n n ^ n
 ~ . . , # # # n n n
. . . , , # # # @ w #
 . . , , , , # # # #

  1:settler(3,0) 2mp   2:warrior(4,0) 2mp
  SELECT found_city(1, 'Roma')
  SELECT end_turn() AS turn
```

## Running it

```sh
make deps      # pip install psycopg
make up        # start postgres in docker
make reset     # load sql/*.sql and deal a world
make play      # attach the client
make sql       # a psql prompt on the same live game
```

`make reset` is destructive and instant — it is how you start over.

## What is where

```
sql/01_schema.sql   the tables. every fact the game knows
sql/02_hex.sql      hex geometry and the terrain function
sql/03_seed.sql     the rulebook: terrain yields, unit stats, the tech tree
sql/04_views.sql    everything derived: the economy, the tech frontier, movement
sql/05_rules.sql    the four things you can do: new_game, found_city, move_unit, end_turn

client/db.py        connection, and echoing statements to the screen
client/render.py    rows to characters; touches no database
client/civ.py       the command loop, with every query it uses in one block
```

Roughly 400 lines of SQL and 300 of Python. It is meant to be read in an hour.

## Commands

| | |
|---|---|
| `n [radius] [seed]` | new game |
| `m <unit> <q> <r>` | move a unit |
| `c <unit> <name>` | found a city with a settler |
| `t [tech]` | list, or choose, research |
| `s <unit>` | highlight where a unit can go |
| `e` | end the turn |
| `: <sql>` | **run any SQL against the live game** |
| `?` `q` | help, quit |

## The parts worth reading

**The economy is one query.** `city_yield` in `sql/04_views.sql` is an aggregate
over the tiles each city works. There is no other source of food, production or
gold anywhere in the codebase. A city works its best *population* tiles, which
is a window function ranking hexes within each city.

**The tech tree is relational division.** "Which techs can I research now?"
means *ones I don't know, all of whose prerequisites I do know* — universal
quantification, encoded as the doubly nested `NOT EXISTS` in `available_tech`.

**A turn is six statements.** `end_turn()` in `sql/05_rules.sql`. None of them
mentions a particular city, unit or civ; each operates on every row at once.
Read it next to the loop you would have written instead.

**Movement is a recursive walk.** `reachable()` expands over hex adjacency
accumulating terrain costs. It is deliberately the naive formulation — it
re-expands hexes reached by several paths and takes the minimum at the end.

**The database refuses illegal moves.** `unit_one_per_tile` is a unique index,
not a check in the client. Try walking one unit onto another and read the error.

## Demos

```sh
make demo-economy    # the yield query, the tiles behind it, and its plan
make demo-scale      # 270k tiles: index lookup vs sequential scan
```

`demo-scale` deals a 270,901-hex world and runs the lookup the renderer does on
every frame, with and without the `(q, r)` index. On the machine this was
written on: **0.039 ms indexed, 19 ms unindexed.**

## What is not built yet

This is the first vertical slice. Deliberately missing:

- **Fog of war.** A `visibility` table and a `visible_world` view keyed on a
  session variable, so each player queries the same tables and sees a different
  map.
- **Simultaneous multi-player turns.** `new_game` already accepts a civ count
  and `end_turn()` is written set-at-a-time over all civs, so the schema is
  ready; what is missing is per-player sessions and the isolation-level demos
  (two units onto one hex; write skew leaving a city undefended).
- **Combat.** Units cannot attack.
- **Production queues.** Cities grow but cannot build anything.

## Licence

Course material. Do as you like with it.
