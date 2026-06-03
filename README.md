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

| Data        | Source                                                  |
| ----------- | ------------------------------------------------------- |
| Teams       | `nflfastR-data/teams_colors_logos.csv`                  |
| Games       | `nfldata/data/games.csv` (schedules + results)          |
| Player stats| `nflverse-data` `stats_player` release (weekly)         |
| Team stats  | `nflverse-data` `stats_team` release (weekly, for DST)  |
| Players     | `nflverse-data` `rosters` release (biographical)        |

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

## Fantasy scoring

The target league is **Half-PPR** (1 QB / 2 RB / 2 WR / 1 TE / 1 FLEX / 1 K /
1 DEF / 5 bench). Scoring lives in `src/fantasy_football/scoring.py`:

- **Offense/kicker** scoring is a linear combination of stats, so it's computed
  in SQL for fast leaderboards. A single `_terms()` map drives both the Python
  (`score_stats`) and SQL (`score_expression`) scorers so they can't drift.
- **Field goals are distance-based** (min 2, no max): 0-29=2, 30-39=3, 40-49=4,
  50-59=5, 60+=6, scored from per-bucket made-FG columns.
- **Team defense (DST)** uses tiered points-allowed and yards-allowed plus event
  scoring (sack/INT/fumble recovery/TD/safety); tiers are non-linear so DST is
  aggregated in Python (`score_team_defense`, `team_defense_season_leaders`).

```bash
python -m fantasy_football.cli leaders --year 2024 --scoring half_ppr --position RB
python -m fantasy_football.cli leaders --year 2024 --position DST
```

Presets: `standard`, `half_ppr` (default), `ppr`. Negative plays use Yahoo
defaults: interception thrown −1, fumble lost −2 (Yahoo has no QB sack penalty;
sacks score +1 on defense).

## Auction values

`src/fantasy_football/valuation.py` turns historical scoring into auction dollar
values for a 12-team / $200 league (configurable). No projection model —
last-season production is the value signal. The pipeline:

1. **Value** — each player/defense summarized three ways for comparison:
   last-season total, last-season PPG, and a weighted 3-year average.
2. **Tier** — a 1-D k-means gives an automated tier per position as a starting
   rating. Real tiers are a *manual* input (Yahoo base prices + head-to-head
   preferences) supplied via a CSV override.
3. **Price** — value over replacement (replacement levels from the roster,
   including an RB/WR/TE flex pool), scaled to the budget pool and smoothed
   within each tier.

```bash
# Panel of values (all three bases shown); pricing uses --basis (default w3yr)
python -m fantasy_football.cli values --year 2025 --position RB

# Export the full table, hand-edit the manual_tier column, then re-price from it
python -m fantasy_football.cli values --year 2025 --export tiers.csv
python -m fantasy_football.cli values --year 2025 --tiers-file tiers.csv
```

## Roadmap

- [x] Data ingestion from nflverse (teams, games, players, weekly + team stats)
- [x] Reference data: load all franchises and seasons 2020-present
- [x] Fantasy scoring (offense + distance-based K + tiered DST), Half-PPR default
- [x] Auction values from tiers (VOR + flex, k-means tiers, manual override)
- [ ] Manual-tier workflow: import Yahoo base prices to seed/blend tiers
- [ ] Play-by-play enrichment (return TDs by type, passer rating)
- [ ] Migrations (Alembic) once the schema stabilizes
