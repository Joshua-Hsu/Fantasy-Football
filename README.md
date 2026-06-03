# Fantasy Football

A football statistics database. It starts as a general-purpose football stats
store — modeled on the structure used by sites like
[Pro Football Reference](https://www.pro-football-reference.com/) — and will
grow fantasy-specific features (scoring, projections, leagues) on top of that
foundation.

## Status

Base schema **plus working ingestion**. The database can be populated with real
NFL data (teams, games, players, per-player box-score lines) from
[nflverse](https://github.com/nflverse). Fantasy scoring comes next.

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

# 3. Load real data from nflverse (2020 through the most recent played season)
python -m fantasy_football.cli load-seasons --start 2020
#   ...or a single season:
python -m fantasy_football.cli load-season --year 2024

# 4. Inspect it
python -m fantasy_football.cli info
```

Ingestion requires the optional `ingest` extra (`pip install -e ".[ingest]"`).
Loaders are **idempotent** — re-running a season refreshes scores and stats in
place rather than duplicating rows, so it's safe to re-run during a season.

Override the database location with the `FANTASY_FOOTBALL_DB` environment
variable or the `--db` flag:

```bash
python -m fantasy_football.cli --db /tmp/ff.db init-db
```

## Data sources

Data is pulled directly from the files [nflverse](https://github.com/nflverse)
publishes on GitHub — no scraping, no rate-limited hosts:

| Data    | Source                                              |
| ------- | --------------------------------------------------- |
| Teams   | `nflfastR-data/teams_colors_logos.csv`              |
| Games   | `nfldata/data/games.csv` (schedules + results)      |
| Stats   | `nflverse-data` `stats_player` release (weekly)     |
| Players | `nflverse-data` `rosters` release (biographical)    |

These trace back to the NFL's own game feeds; nflverse cleans and republishes
them for programmatic use. See `src/fantasy_football/ingest/nflverse.py`.

> **Coverage note:** weekly stats cover passing, rushing, receiving, fumbles,
> kicking (FG/PAT) and return yardage. Return touchdowns aren't split by type
> in the source and passer rating isn't provided, so those columns stay at
> their defaults pending a future play-by-play enrichment pass.

## Project layout

```
src/fantasy_football/
  models.py          # SQLAlchemy ORM models (the schema)
  db.py              # engine/session helpers, SQLite config
  cli.py             # init-db / info / load-* admin commands
  ingest/nflverse.py # nflverse loaders
tests/
  test_schema.py
  test_ingest.py
```

## Running tests

```bash
pytest
```

## Roadmap

- [x] Data ingestion from nflverse (teams, games, players, weekly stats)
- [x] Reference data: load all franchises and seasons 2020-present
- [ ] Play-by-play enrichment (return TDs, defensive stats, passer rating)
- [ ] Fantasy scoring rules and computed fantasy points
- [ ] Standings / season aggregate views
- [ ] Migrations (Alembic) once the schema stabilizes
