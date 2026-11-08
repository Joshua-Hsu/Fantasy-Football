# Build 4: the state files as durable memory - schema, validation, roster command

Written 2026-11-08.

## Why
`roster.csv`, `decisions.csv`, `injuries.csv`, `defense_profiles.csv` and
(after build 2) `PREFERENCES.md` are the memory a fresh session relies on.
They are hand-appended and have drifted before (a 10-column row broke the
log; the roster was wrong for ten days). They need a schema, a validator and
a safe way to change the roster.

## Decisions (owner)
1. New module `src/fantasy_football/state.py` and CLI commands:
   - `cli check-state`: validates every state file and exits non-zero with
     a per-row message on any problem. Rules: `decisions.csv` exactly 9
     columns per row, date ISO, week int, who in {claude,user},
     verdict in {pending,hit,miss,neutral}, override text starts with
     yes/no (case-insensitive); `injuries.csv` 9 columns, date ISO;
     `roster.csv` header `player,pos,team,slot`, pos in {QB,RB,WR,TE,K,DST},
     team is a known code, every player resolves to a DB player (DST rows
     resolve to a team) - print unmatched names; `defense_profiles.csv`
     3 columns, team code <= 3 chars. Also warn (not fail) when `roster.csv`
     has more than 15 non-IR players or fewer than 9 starters' worth of
     positions.
   - `cli roster show|add|drop|move`: `show` prints the roster grouped by
     slot; `add NAME [--slot S]`, `drop NAME`, `move NAME --slot S` edit
     `roster.csv` in place preserving column order and line endings, resolve
     the name against the DB (fail on no match or ambiguity, print the
     candidates), and append a one-line `who=user` row to `decisions.csv`
     ("Roster: added X / dropped Y") so every roster change is logged. No
     other code path writes `roster.csv`.
   - `cli log add --week N --who claude|user --decision ... --board ...
     --override ... --rationale ...`: appends a correctly shaped
     `decisions.csv` row (outcome/verdict = pending), so sessions stop
     hand-writing CSV. `cli log grade --row I --outcome ... --verdict ...`
     fills a pending row; refuses a non-pending one unless `--force`.
2. Line endings and quoting: write with `csv.writer(..., lineterminator="\n")`
   and `QUOTE_MINIMAL`; read with `newline=""`; a round-trip of an untouched
   file must produce zero diff (test it on the real files).
3. Add `check-state` to the test suite (a test that runs it on the committed
   files and expects success) and to `dvp.yml` as a step before the build so
   a broken row fails the Action loudly instead of silently dropping rows.
4. Document the schemas in a short `STATE.md` at the repo root (one table
   per file: column, type, rule, example) and point CLAUDE.md at it in the
   "In-season decision discipline" section (replace the inline column lists
   with a reference).

## Deliverables
- `state.py`, the three CLI commands, `tests/test_state.py` (offline; uses
  tmp copies of small CSVs and the seeded in-memory DB pattern), `STATE.md`,
  CLAUDE.md pointer, the `dvp.yml` step.
- Run `cli check-state` on the real repo files and fix any existing row
  that fails by the minimal edit (report what you changed in the PR).
- `pytest` fully green.
