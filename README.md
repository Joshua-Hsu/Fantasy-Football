# Fantasy Football

A football statistics database. It starts as a general-purpose football stats
store — modeled on the structure used by sites like
[Pro Football Reference](https://www.pro-football-reference.com/) — and will
grow fantasy-specific features (scoring, projections, leagues) on top of that
foundation.

## Status

Foundation / base schema. The data model and database tooling are in place;
data ingestion and fantasy scoring come next.

## Data model

The schema is normalized around the core entities of a football stats site:

| Table               | Grain                            | Purpose                                            |
| ------------------- | -------------------------------- | -------------------------------------------------- |
| `teams`             | one franchise                    | Team identity, conference/division                 |
| `seasons`           | one year                         | Season metadata (e.g. number of weeks)             |
| `players`           | one person                       | Biographical info + external `slug` identifier     |
| `games`             | one matchup                      | Two teams, score, date, season type                |
| `player_game_stats` | one player in one game           | Box-score line: passing/rushing/receiving/kicking  |
| `team_game_stats`   | one team in one game             | Team totals: yards, turnovers, time of possession  |

Design notes:

- **Counting stats default to `0`**, not `NULL`, so aggregation and fantasy
  scoring never have to special-case missing values. Rate stats (passer
  rating) stay nullable because "no attempts" isn't zero.
- **`player_game_stats` is the workhorse grain** — most fantasy scoring is
  derived from per-player, per-game lines.
- **External identifiers** (`players.slug`, `teams.abbreviation`) give clean
  keys to de-duplicate against when ingesting from external sources.
- **Foreign keys are enforced** (SQLite `PRAGMA foreign_keys = ON` is set on
  every connection).

## Getting started

```bash
# 1. Create a virtual environment and install
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# 2. Create the database (data/fantasy_football.db by default)
python -m fantasy_football.cli init-db

# 3. Inspect it
python -m fantasy_football.cli info
```

Override the database location with the `FANTASY_FOOTBALL_DB` environment
variable or the `--db` flag:

```bash
python -m fantasy_football.cli --db /tmp/ff.db init-db
```

## Project layout

```
src/fantasy_football/
  models.py   # SQLAlchemy ORM models (the schema)
  db.py       # engine/session helpers, SQLite config
  cli.py      # `init-db` / `info` admin commands
tests/
  test_schema.py
```

## Running tests

```bash
pytest
```

## Roadmap

- [ ] Data ingestion from external stat sources (e.g. Pro Football Reference)
- [ ] Reference data: load all current teams and a season
- [ ] Fantasy scoring rules and computed fantasy points
- [ ] Standings / season aggregate views
- [ ] Migrations (Alembic) once the schema stabilizes
