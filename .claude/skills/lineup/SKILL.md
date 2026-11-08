---
name: lineup
description: Set this week's fantasy lineup - run `cli lineup`, sweep injuries for every rostered player, state the lineup slot by slot with the board's reasons and the owner's PREFERENCES applied, name each questionable starter and his swap, and log a "FINAL week-N lineup" row. Use when the owner says "/lineup", "set my lineup", "who do I start", "start/sit", "final lineup for week N", asks about a FLEX or a questionable starter, or hands over his own lineup to check.
---

# /lineup - this week's starting lineup

The answer must come out the same from any session: the command makes the
board call, the sweep adjusts for news, PREFERENCES.md adds the owner's
standing calls, and the row in `decisions.csv` pins it before kickoff.
Run all sections in order. Do not narrate routine steps; speak mid-task only
with information the owner needs.

## Setup (identical in /lineup, /waivers, /sweep)

```bash
export SSL_CERT_FILE=/root/.ccr/ca-bundle.crt
git fetch origin main
git checkout -B claude/lineup-wk<N> origin/main   # or the branch the session was given
python -c "import fantasy_football, pandas, pyarrow, openpyxl, flask" \
  || pip install -e ".[dev,ingest,export,web]"
python -m fantasy_football.cli info
python - <<'EOF'
import os, sqlite3
c = sqlite3.connect(os.environ.get("FANTASY_FOOTBALL_DB", "data/fantasy_football.db"))
for t in ("teams", "players", "games", "player_game_stats", "team_game_stats"):
    print(t, c.execute(f"select count(*) from {t}").fetchone()[0])
print("season, last week with stats:", c.execute("select g.season_year, max(g.week) from games g "
      "join player_game_stats p on p.game_id = g.id group by 1 order by 1 desc limit 1").fetchone())
print("players with current_team:", c.execute(
      "select count(*) from players where current_team is not null").fetchone()[0])
EOF
```

Healthy (2025 + 2026 loaded, Nov 2026): 36 teams, ~3,800 players, ~28k
player_game_stats, last week = the last played week, 2,500+ players with
`current_team`. If `info` says `Tables (0)`, any count is 0, the last week
is behind the schedule, or `current_team` is empty, rebuild (2-4 min,
idempotent; `load-season` from the CLI fails here, see Known traps):

```bash
python -c "from fantasy_football.ingest import nflverse as nv; from fantasy_football.db import *; nv.TEAMS_URL='https://raw.githubusercontent.com/nflverse/nflfastR-data/master/teams_colors_logos.csv'; e=create_db_engine(); init_db(e); S=get_sessionmaker(e)(); nv.load_season(S,2026); nv.load_season(S,2025)"
python -m fantasy_football.cli load-active --year 2026
```

(Season year, not calendar year: in January use the season that started in
September, and the year before it as the baseline.) If
`python -c "import fantasy_football; print(fantasy_football.__file__)"`
points outside this checkout (a shared venv from another worktree), prefix
every `python -m fantasy_football.cli` call with `PYTHONPATH=src`.

Read, in this order, before saying anything about players:
1. `PREFERENCES.md` - the owner's standing calls (holds, waiver rules,
   lineup rules, reporting preferences, rejected ideas). If it is missing,
   say so and work from PLAYBOOK.md alone.
2. `PLAYBOOK.md` - the checklist and the graded misses.
3. `roster.csv` (`player,pos,team,slot`) - **read it back to the owner**
   (player, pos, team, slot, one line each) and ask for corrections before
   any recommendation. It changes only on the owner's word.

Rebuild the sidecar (box scores, Vegas, official report, profiles):

```bash
python -m fantasy_football.cli dvp --year 2026 --baseline-year 2025 --out docs/dvp.js
```

`warning: scheme layer participation missing: HTTP Error 404` is expected
until the season's participation file is published. Read the payload with
`json.loads(open("docs/dvp.js").read().split("window.FF_DVP = ", 1)[1].strip().rstrip(";"))`;
`week` is the coming week.

## 1. Run the command

```bash
python -m fantasy_football.cli lineup            # --json for machine output, --week N for labels
```

It reads `roster.csv`, `docs/dvp.js` and the DB and prints, per slot (QB,
RB x2, WR x2, TE, FLEX, K, DST), the winner and the top-3 alternatives with
opponent, season rank / last-3 rank / trend for his position, implied total,
last-3 line, model projection and flag; then an "unavailable" list (OUT,
DOUBTFUL, IR, bye) and "decisions to watch" (questionable starters and the
swap). If it exits on a missing/old sidecar, rerun the `dvp` step; if it
prints unmatched roster names, fix nothing yourself - show the names to
the owner. If the command does not exist yet (`invalid choice: 'lineup'`),
say so and build the same table by hand from the sidecar plus
`cli usage --player "<name>"` for every rostered player.

## 2. Injury sweep for the roster

For every rostered player AND every likely FLEX/bench swap:
1. Official designation - the sidecar's `official` list:
   ```bash
   python - <<'EOF'
   import csv, json
   s = open("docs/dvp.js").read()
   d = json.loads(s.split("window.FF_DVP = ", 1)[1].strip().rstrip(";"))
   names = {r["player"].lower() for r in csv.DictReader(open("roster.csv", newline=""))}
   for o in d["official"]:
       if o["n"].lower() in names:
           print(o["n"], o["tm"], o["st"], o["inj"], o.get("prac", ""))
   EOF
   ```
2. The analysis sheet - newest `injuries.csv` row per player (timeline,
   replacement, scheme note).
3. News since the report - **WebSearch** (WebFetch to news sites is
   egress-blocked): `"<player> injury status week <N>"`, inactives on game
   day. Quote the source and its time.

Append an `injuries.csv` row (ship block) for anything new: status,
timeline, who absorbs the work, what the scheme does with it. Never treat
"no timeline" as "back next week". The command only knows the official
designations baked into the sidecar at the last `dvp` run: if the report has
refreshed since, rerun `cli dvp` then `cli lineup`; news newer than any
report is applied by hand in section 3 and labeled "news after the report".

## 3. State the lineup

Slot by slot, one line each, in this order: QB, RB1, RB2, WR1, WR2, TE,
FLEX, K, DST. Each line: the starter, the opponent, the reasons the command
printed (rank/last-3/trend, implied total, last-3 line, proj), and the
runner-up with why he lost. Then:
- **PREFERENCES applied**: name each standing call that touched the
  lineup (e.g. a hold such as Corum stays rostered even when benched; the
  owner's rivalry tiebreak; Kamara over Corum as the RB fallback when
  Rhamondre is out) and whether it changed the board's order. A preference
  that overrules the board is written **OVERRIDE (owner preference: ...)**.
- **OVERRIDE** for any slot where you disagree with the command: exactly
  one structural reason (injury, depth chart, measured scheme). No
  structural reason = the board wins. Usage claims need two games or a
  structural cause.
- **Questionable starters**: every starter with Q/DNP/limited, the swap
  (next man up for that slot, from the command), and the decision time
  (inactives ~90 minutes before his kickoff; note early/late games).
- **Unavailable**: the command's list with reasons.

If the owner phrases a preference as a question ("is X not a play?"), run
the full case for X before answering.

## 4. Log it

Append ONE row to `decisions.csv` before the first kickoff:
- `decision`: `FINAL week-<N> lineup: QB ...; RB ..., ...; WR ..., ...; TE ...; FLEX ...; K ...; DST ...` plus what changed from the owner's current lineup.
- `who`: `claude`; `board`: the grade line for every starter and the
  displaced alternative; `override`: `no` or `yes - <reason>`;
  `rationale`: the close calls in prose; `outcome`, `verdict`: `pending`.

**If the owner has given his own lineup**: log it as a separate row with
`who=user` (`USER: final week-<N> lineup ...`), and in your answer flag
only the slots where it differs from the command, with the board grade for
both players. Do not re-argue slots that match.

When news after the log changes the call, restate from zero, say whether it
reverses the logged row, and append a new FINAL row (never edit the old one).

## Ship block (identical in /lineup, /waivers, /sweep)

1. **Append rows, never edit old ones** (the one exception is /sweep's
   grading, which fills `outcome`/`verdict` of `pending` rows). Use Python's
   csv module so quoting stays valid:
   ```bash
   python - <<'EOF'
   import csv
   rows = [  # date,week,decision,who,board,override,rationale,outcome,verdict
       ["2026-11-15", "10", "FINAL week-10 lineup: ...", "claude", "...", "no", "...", "pending", "pending"],
   ]
   with open("decisions.csv", newline="") as f:
       assert f.read().endswith("\n")            # never glue a row onto the last line
   with open("decisions.csv", "a", newline="") as f:
       csv.writer(f, lineterminator="\n").writerows(rows)
   EOF
   ```
   `injuries.csv` is the same with `date,player,team,pos,status,injury,timeline,replacement,note`.
   If `python -m fantasy_football.cli log add` exists it writes the same
   shape; either is fine. Never rewrite `injuries.csv` or
   `defense_profiles.csv` wholesale (their old rows do not round-trip
   byte-for-byte); append only.
2. Regenerate the sidecar so `#/log` and `#/injuries` show the new rows,
   then bump the cache-buster to the minute (UTC, as `dvp.yml` does):
   ```bash
   python -m fantasy_football.cli dvp --year 2026 --baseline-year 2025 --out docs/dvp.js
   sed -i -E "s/(dvp\.js\?v=)[0-9]+/\1$(date -u +%Y%m%d%H%M)/" docs/index.html
   grep -n 'dvp.js?v=' docs/index.html
   ```
3. `pytest` - must be green before committing.
4. Commit only what you changed (`decisions.csv`, `injuries.csv`,
   `defense_profiles.csv`, `docs/dvp.js`, `docs/index.html`, and
   `roster.csv` only if the owner ordered a change; never `data/`), ending
   the message with the attribution lines this session was given.
5. Push with a lease and retry on server errors:
   ```bash
   B=$(git branch --show-current)
   git fetch origin "$B" 2>/dev/null || true      # refresh the lease ref (container resets leave it stale)
   for d in 0 2 4 8 16; do sleep $d; git push --force-with-lease -u origin "$B" && break; done
   ```
   Retry only on 5xx / connection errors. A `stale info` rejection is not a
   server error: `git fetch origin "$B"`, check what is there, push again.
6. Open a PR to `main` and squash-merge it. `gh` may be unauthenticated in
   the sandbox; use the GitHub MCP tools: `mcp__github__create_pull_request`
   (owner `Joshua-Hsu`, repo `Fantasy-Football`, base `main`, head `$B`),
   then `mcp__github__merge_pull_request` with `merge_method: "squash"`.
7. Realign and verify:
   ```bash
   git fetch origin main
   git reset --hard origin/main
   git push --force-with-lease origin "$B" || true
   git show origin/main:decisions.csv | tail -2     # your row must be there
   ```

**Merge conflict in generated files** (`docs/dvp.js`, `docs/data.js`,
`docs/index.html` - usually the `dvp.yml` Action committed to main while
you worked):
```bash
git fetch origin main
git rebase origin/main
git checkout --theirs docs/dvp.js docs/index.html   # whichever conflicted (+ docs/data.js); content is regenerated next
python -m fantasy_football.cli dvp --year 2026 --baseline-year 2025 --out docs/dvp.js
sed -i -E "s/(dvp\.js\?v=)[0-9]+/\1$(date -u +%Y%m%d%H%M)/" docs/index.html
git add docs/dvp.js docs/index.html
GIT_EDITOR=true git rebase --continue
```
(In a rebase `--theirs` is your commit's side; it does not matter which
side you take because the regeneration overwrites it from the merged CSVs.
`docs/data.js` is rebuilt by `build-webapp`; take main's side
(`--ours` during a rebase) when you did not change tiers.) A conflict in a CSV is NOT generated: both
sides are appends, so keep main's rows and then yours, in date order, and
delete the markers. Then push (step 5) and merge again.

## Known traps

- **TEAMS_URL redirect**: `nflverse.TEAMS_URL` (github.com/.../raw/...)
  redirects and fails in this sandbox, so `cli load-season(s)` dies; the
  rebuild one-liner above patches it to raw.githubusercontent.com.
- **Stale remote ref after a container reset**: `--force-with-lease`
  compares against the local remote-tracking ref, which a reset container
  may not have or may hold stale. `git fetch origin <branch>` first.
- **DST implied total**: `vegas[TEAM].it` is that team's OWN implied
  total. A defense is graded on its opponent's: `vegas[<opp code>].it`
  (the Ravens error, wk9 2026: Baltimore's own 16.5 was read as the
  opponent's; Jacksonville's was 25.5).
- **One game is not a role**, both ways - unless it ends a trend or has a
  structural cause (Mike Washington wk9 2026: 25 carries were the end of an
  eight-week climb the coach owned, not a spike). Say which case it is.
- **Names in prose are checked against `Player.current_team`** (the
  Pickens/`validate_next` lesson: a note named George Pickens, a Cowboy, on
  Pittsburgh). Before naming any player's team or role, confirm with
  `cli usage --player` or the DB; the check only works after `load-active`.
- **`roster.csv` changes only on the owner's word.** Never edit it from a
  league log, a waiver result you inferred, or a stale slot note.
- Snap counts are not in the DB; the snap file lags box scores by a day.
  "No stat line" is not DNP. Do not invent snap shares.
