# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project vision

This is a **fantasy football auction-draft toolkit**, built bottom-up on top of
a general football stats database. The end goal is to produce, for a given
scoring system and league size, **per-player auction values** (dollar values
for a budget auction draft) and **positional tiers** (groupings of players of
comparable value, for tier-based drafting).

The work is layered, and the lower layers exist before the upper ones:

1. **Stats database** (done) — normalized schema of teams/seasons/players/games
   and per-player + per-team box-score lines.
2. **Ingestion** (done) — loads real NFL data from nflverse.
3. **Fantasy scoring** (done) — `scoring.py`; Half-PPR default; offense + K + DST.
4. **Auction values from tiers** (done) — `valuation.py`. Note: the projection
   model was intentionally skipped; historical production is the value signal and
   feeds tiers → dollar values directly.

Negative plays use **Yahoo defaults**: INT thrown −1, fumble lost −2 (no QB sack
penalty; defensive sack is +1).

League settings: **Half-PPR**, roster 1 QB / 2 RB / 2 WR / 1 TE / 1 FLEX
(RB/WR/TE) / 1 K / 1 DEF / 5 bench — relevant for replacement levels when
computing auction values.

When adding features, respect this layering: scoring reads stat lines,
projections read scoring, auction values read projections.

## Commands

Setup (the ingestion layer needs the `ingest` extra — pandas + pyarrow):

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,ingest]"
```

Database lifecycle (CLI lives in `src/fantasy_football/cli.py`):

```bash
python -m fantasy_football.cli init-db                 # create tables
python -m fantasy_football.cli load-seasons --start 2020   # load 2020..latest played
python -m fantasy_football.cli load-season --year 2024     # one season
python -m fantasy_football.cli load-teams                  # franchises only
python -m fantasy_football.cli info                        # DB path + tables
```

A full 2020-present load takes ~50s and writes ~112k stat lines. The DB defaults
to `data/fantasy_football.db` (gitignored); override with `--db PATH` or
`$FANTASY_FOOTBALL_DB`. `:memory:` is supported for ephemeral use.

Tests (pytest, configured in `pyproject.toml` with `pythonpath = ["src"]`):

```bash
pytest                                  # whole suite
pytest tests/test_ingest.py             # one file
pytest tests/test_ingest.py::test_reload_is_idempotent   # one test
```

Tests are **fully offline** — `test_ingest.py` monkeypatches the four nflverse
fetchers with small in-memory DataFrames. Never add a test that hits the
network.

## Architecture

**Schema (`models.py`).** SQLAlchemy 2.0 typed models. The grain that matters is
`PlayerGameStats` — one box-score line per (player, game) — because fantasy
scoring is computed from it. Two deliberate conventions:

- **Counting stats default to `0`, not NULL** (see the `_count()` helper), so
  aggregation and scoring never special-case missing values. Rate/bio fields
  stay nullable.
- **External identifiers are the join keys**: `players.slug` holds the nflverse
  player id (GSIS id), `teams.abbreviation` holds the nflverse team code. These
  are how ingestion de-duplicates on re-runs.

**Ingestion (`ingest/nflverse.py`).** Loaders are **idempotent**: every row is
matched to an existing record by natural key and updated in place, so
re-running a season refreshes scores/stats rather than duplicating. Key points
a future instance must know:

- **Do not use `nfl_data_py`'s functions for schedules or weekly stats.** Its
  schedule URL (`habitatring.com`) is blocked by the web sandbox's network
  policy, and its weekly-stats URL points at a stale release missing recent
  seasons. This module instead reads nflverse's GitHub files directly via pandas
  (URLs are constants at the top of the file). Only GitHub-hosted nflverse URLs
  are reachable here.
- **Weekly stats carry no game id.** Each stat row is matched to a `Game` by
  `(week, frozenset({team_id, opponent_team_id}))`. This relies on schedule and
  stats sources agreeing on team codes and postseason week numbers (19-22) —
  they do, verified across all rows.
- **`season_has_data(year)`** drives "most recent season": future/unplayed years
  exist in the schedule but 404 on the stats file, so `load_seasons` skips them
  and the range resolves to the latest *played* season automatically.
- **NaN hygiene**: source DataFrames use NaN/NaT for missing values; the
  `_count` / `_opt_*` helpers convert these before values reach the ORM.

**Coverage gap to be aware of:** weekly stats include passing, rushing,
receiving, fumbles, kicking (FG/PAT) and return *yardage*, but **not** return
touchdowns (not split by type in the source) or passer rating. Those columns sit
at their defaults pending a future play-by-play enrichment pass. Don't assume
they're populated.

**Scoring (`scoring.py`).** Offense + kicker scoring is a **linear** combination
of stat columns, so a single `_terms()` map builds both `score_stats` (Python,
one object) and `score_expression` (a SQL expression for fast `func.sum`
leaderboards) — they cannot drift. Field goals are **distance-based**, scored
from per-bucket made-FG columns (`fg_made_0_19 … fg_made_60_plus`), which keeps
kicker scoring linear/SQL-friendly. **Team DST** scoring is **non-linear**
(tiered points-allowed and yards-allowed), so it's computed per-game in Python
(`score_team_defense`, `team_defense_season_leaders`), not via SQL. The default
rules are `HALF_PPR`; presets are `standard`/`half_ppr`/`ppr`. INT/fumble
penalties are placeholders (-2) pending final league values.

**Valuation (`valuation.py`).** Builds auction dollar values from history, no
projection model. `compute_values()` returns per-position `ValueRow`s with all
three value bases (total / ppg / w3yr), a k-means `kmeans_tier`, an effective
`tier` (manual override if supplied), `vor`, and `dollars`. Pricing is VOR with
replacement levels from `LeagueConfig` (a real RB/WR/TE **flex pool** in
`_replacement_values`), scaled to the budget pool and **smoothed within tier**.
k-means is pure-Python (`kmeans_1d`) so this module needs no extra dependency.
Manual tiers (the league's real tiers, from Yahoo prices + H2H) come in via the
CLI `values --export`/`--tiers-file` CSV round-trip, keyed by entity `key`
(`p<player_id>` / `d<team_abbr>`).

**User ratings / H2H game (`ratings.py` + `web.py`).** The league's real tiers
are produced by a head-to-head comparison game: a human picks Player A vs B and
each pick updates an Elo-style **user rating** (`ratings.py`), seeded from the
computed value so picks only *refine* the order. `record_pick` logs to the
`comparisons` table and updates `user_ratings`; `next_matchup` favours
similarly-rated same-position players; `user_rating_tiers` runs k-means on the
ratings to produce `{key: tier}` that flows into `compute_values(manual_tiers=)`
(also via `values --use-user-ratings`). `web.py` is a self-contained Flask app
(optional `web` extra) launched with `python -m fantasy_football.cli serve`;
both new tables are created by `create_all`, so no DB rebuild is needed.

**Draft board (`export.py`).** `cheatsheet` CLI writes an .xlsx (optional
`export` extra, openpyxl): static per-position tier sheets plus a live **Draft
Board** sheet whose recommended prices are Excel formulas — marking a player
drafted + the price they went for re-prices the rest for auction inflation
(allocation weight = recommended − $1; remaining pool/slots shrink). Pure
in-sheet formulas, no recompute needed.

**Master tiers + weekly loop.** `master_tiers.<date>.csv` is the base used
everywhere. It carries a continuous **`rating`** column (the user rating from the
pick game) alongside the integer `manual_tier` (derived from the rating via
`assign_sized_tiers`) and prices. The flow is a cycle: `build-webapp
--tiers-file <master>` seeds the app's Elo from each player's `rating`
(`seed_overrides`), so play *refines the master* rather than starting from raw
value; the app's **Commit to GitHub** button POSTs each user's `rating` rows to
a serverless proxy (`infra/commit-worker/`, a Cloudflare Worker holding the
write token) that writes `picks/u-<id>.csv` — one stable file per browser, so
re-submits overwrite (one vote each) and `picks/` accumulates as a persistent
user-rankings database. (The app's **Export** button is now just a personal CSV
download, not part of the pipeline.) Two GitHub Actions close the loop:
**Rebuild Master Tiers** (`master-tiers.yml`, run manually) runs `import-tiers
--file picks/*.csv --prices-from <prev master>` which **averages the `rating`
across all pick files** (one vote each via `_merge_ratings`), folds them onto the
previous master (un-picked players carry forward), re-derives tiers, and
regenerates `docs/data.js`; it does **not** clear `picks/`, so it's idempotent
and can run repeatedly as users submit. **Archive Week** (`archive-week.yml`)
runs **weekly** (cron, Tuesdays) — it snapshots the current master + `picks/*`
into `archive/<date>/` and then **removes the pick files from `picks/`**, so each
week's rebuilds drop last week's rankings and fresh submissions accumulate from
empty. `import-tiers` falls
back to a file's integer `manual_tier` only for legacy exports with no `rating`.

**Schema-change note:** `init-db` uses `create_all`, which does **not** alter
existing tables. When you add columns (as the scoring work did to
`PlayerGameStats` FG buckets and `TeamGameStats` defensive fields), an existing
`data/*.db` must be deleted and reloaded — fine, since the DB is gitignored and
fully reproducible via the loaders in ~50s.

**`db.py`** centralizes engine/session creation and enables SQLite foreign-key
enforcement (`PRAGMA foreign_keys = ON`) per connection — FKs are off by default
in SQLite, so anything bypassing this helper loses referential integrity.

## Data sources

All from nflverse's GitHub-published files (no scraping): teams from
`nflfastR-data`, schedules/results from `nfldata`, weekly stats from the
`stats_player` release, rosters from the `rosters` release. See the URL
constants in `ingest/nflverse.py`.
