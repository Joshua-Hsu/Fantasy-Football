# Build 1: `cli lineup` - the lineup answer as a command

Written 2026-11-08. Owner decisions are final; everything else is the
builder's call with CLAUDE.md / PLAYBOOK.md as the tie-breaker.

## Why
Lineup questions are currently answered from one long chat session's memory.
A fresh session must be able to run ONE command and get the same answer,
with the reasons, so the judgment does not drift between sessions.

## Decisions (owner)
1. New CLI command `lineup` in `src/fantasy_football/cli.py`, logic in a new
   module `src/fantasy_football/lineup.py`. No LLM, no network beyond what
   `cli dvp` already fetches; it reads the committed `docs/dvp.js` sidecar
   (parse the JSON after `window.FF_DVP = `) plus the local DB.
2. Inputs: `roster.csv` (player,pos,team,slot - the ONLY source of truth for
   who is rostered; slot text is free-form and may be ignored), the sidecar
   (`profiles` = season rank / last-3 rank / trend per position, `vegas`
   implied totals, `official` injury designations, `opp` for the week,
   `flags`), and the DB for the last three games of usage per player
   (targets, carries, fantasy points under the default rules) plus season
   PPG. Snap counts are NOT in the DB; if `metrics`/snap data is absent, say
   so, do not invent it.
3. Scoring: port the app's matchup model from `docs/app.js` (`modelProj`:
   `ppg x vegas^.45 x dvp^.35 x pace^.2`, each a clamped ratio to league
   average; DvP blends season and last-3 by `wNow = min(through_week/6, .9)`;
   K keys off own implied total, DST inverts the opponent's). The Python and
   JS numbers must agree to 0.1 on the same inputs - add a test that pins a
   hand-computed case. Use **recent form** explicitly: `ppg` in the model is
   a blend of season PPG and last-3 PPG weighted 50/50 once a player has 3
   games (document the weight as a constant).
4. Lineup slots: 1 QB, 2 RB, 2 WR, 1 TE, 1 FLEX (RB/WR/TE), 1 K, 1 DST
   (CLAUDE.md league settings). Greedy fill by projected points is fine, but
   the FLEX must consider the best remaining RB/WR/TE and the output must
   show the ranked alternatives for every slot (top 3), not just the
   winner.
5. Injury handling: a player whose official designation is OUT, DOUBTFUL,
   IR or who is on a bye is excluded and listed under "unavailable" with the
   reason. QUESTIONABLE / DNP players are included but flagged, and the
   output names the next man up for that slot. `injuries.csv` (newest row
   per player) is shown as a note when present.
6. Output: a Markdown table per slot (player, opp, season rk / last-3 rk /
   trend for his position, implied total, last-3 line, proj, flag) followed
   by an "unavailable" list and a one-line "decisions to watch" list
   (questionable starters and the swap for each). `--json` prints the same
   as JSON for the app or other tools. `--week N` overrides the sidecar's
   week only for labeling; the sidecar is already built for the coming week.
7. Exit cleanly with a clear message when `docs/dvp.js` is missing or
   predates the needed fields, or when `roster.csv` names a player not in
   the DB (print the unmatched names; do not guess).

## Deliverables
- `src/fantasy_football/lineup.py` with pure functions (payload + rows in,
  lineup out) so tests need no network.
- `cli lineup [--roster roster.csv] [--dvp docs/dvp.js] [--json] [--week N]`.
- `tests/test_lineup.py`: offline, builds a small payload dict + seeded DB
  (see `tests/test_matchups.py::_seed` for the pattern): model parity with
  a hand-computed case, OUT/bye exclusion, FLEX picks the best of RB/WR/TE,
  questionable flag + next man up, unmatched roster name reported.
- A short section in CLAUDE.md under "In-season decision discipline"
  describing the command (3-6 lines), and one line in PLAYBOOK.md step 2
  saying the board grade comes from `cli lineup`.
- `pytest` fully green; run `python -m fantasy_football.cli lineup` against
  the real repo data once and paste the output into the PR description.

## Notes for the builder
- Local DB may be empty in a fresh container. Rebuild with:
  `python -c "from fantasy_football.ingest import nflverse as nv; from fantasy_football.db import *; nv.TEAMS_URL='https://raw.githubusercontent.com/nflverse/nflfastR-data/master/teams_colors_logos.csv'; e=create_db_engine(); init_db(e); S=get_sessionmaker(e)(); nv.load_season(S,2026); nv.load_season(S,2025)"`
  then `python -m fantasy_football.cli load-active --year 2026`
  (SSL_CERT_FILE=/root/.ccr/ca-bundle.crt). The plain `load-season` CLI
  fails on the teams URL redirect in this sandbox.
- Names: match roster names to `Player.full_name` case-insensitively, with
  suffix/punctuation normalisation (see `_norm_name` in `scout.py`). DST rows
  use the team code in the player column (e.g. `LA`).
- Do not edit `decisions.csv`, `injuries.csv`, `roster.csv` or the Actions.
