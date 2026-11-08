---
name: waivers
description: Build the weekly waiver sheet from the owner's wire - usage, this week's spot and the rest-of-season schedule through week 16 for every named player, ranked season-long first, bids priced by the field rule, a named drop per claim with shared-drop warnings, then log the sheet and the owner's final queue. Use when the owner says "/waivers", sends waiver-wire screenshots or a list of names, asks "who should I pick up", "what should I bid", "who do I drop for X", or wants a FAAB queue checked.
---

# /waivers - the waiver sheet

A claim is a season-long decision with a price. Every named player gets
the same three numbers (usage, this week, rest of season), every claim
names its drop and its bid, and the sheet is logged before waivers run
(Wednesday). Run all sections in order. Do not narrate routine steps.

## Setup (identical in /lineup, /waivers, /sweep)

```bash
export SSL_CERT_FILE=/root/.ccr/ca-bundle.crt
git fetch origin main
git checkout -B claude/waivers-wk<N> origin/main   # or the branch the session was given
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

## 1. Take the wire

Input is the owner's wire: screenshots or a list of names. Transcribe every
player (name, team, pos, rostered %, any waiver-column note such as "3
teams watching") and read the list back. If the remaining FAAB is not in
the screenshots and a bid above $1 is on the table, ask once. Confirm each
player's team in the DB, not from memory (Known traps: Pickens).

## 2. Evidence for every named player

Run all three for every player the owner named, and for each roster player
who is a candidate drop, so the add and the drop are compared on the same
numbers.

a. **Usage** (PLAYBOOK 1, 2b, 3):
   ```bash
   python -m fantasy_football.cli usage --player "<name>"
   ```
   Quote the last two games (targets/carries/points) and the best games,
   never the mean as a ceiling. One game is a spike unless it ends a trend
   or has a structural cause.

b. **This week's spot** from the sidecar - season rank / last-3 rank /
   trend vs his position, and the implied total (32 = softest defense):
   ```bash
   python - KC:RB LV:RB SEA:DST <<'EOF'
   import json, sys
   s = open("docs/dvp.js").read()
   d = json.loads(s.split("window.FF_DVP = ", 1)[1].strip().rstrip(";"))
   V, P, O = d["vegas"], d["profiles"], d["opp"]
   for arg in sys.argv[1:]:                      # TEAM:POS
       tm, pos = arg.split(":")
       o = O.get(tm, "bye")
       if o == "bye":
           print(f"{tm} {pos}: BYE"); continue
       oc = o[1:]
       own, opp = V.get(tm, {}).get("it"), V.get(oc, {}).get("it")
       if pos in ("K", "DST"):
           print(f"{tm} {pos} {o}: own implied {own} | OPPONENT implied {opp} (DST keys off this)")
           continue
       p = P.get(oc, {})
       print(f"{tm} {pos} {o}: {oc} season rk {p['rk'][pos]} / last-3 rk {p['rk3'][pos]} "
             f"(trend {p['trend'][pos]:+d}; 32 = softest) | {tm} implied {own}")
   EOF
   ```

c. **Rest of season through week 16** - every remaining opponent with its
   season/last-3 rank vs the position, byes, the average last-3 rank, and
   the playoff weeks (15-16, marked `PO`) named:
   ```bash
   python - RB KC,LV <<'EOF'
   import json, os, sqlite3, sys
   pos, teams = sys.argv[1], sys.argv[2].split(",")
   s = open("docs/dvp.js").read()
   d = json.loads(s.split("window.FF_DVP = ", 1)[1].strip().rstrip(";"))
   P, wk = d["profiles"], d["week"]
   c = sqlite3.connect(os.environ.get("FANTASY_FOOTBALL_DB", "data/fantasy_football.db"))
   ab = dict(c.execute("select id, abbreviation from teams"))
   rows = c.execute("select week, home_team_id, away_team_id from games where season_year=? "
                    "and season_type='regular' and week>=? and week<=16", (d["year"], wk)).fetchall()
   for tm in teams:
       sched = {w: ("v" + ab[a] if ab[h] == tm else "@" + ab[h])
                for w, h, a in rows if tm in (ab[h], ab[a])}
       out, r3 = [], []
       for w in range(wk, 17):
           o = sched.get(w)
           if not o:
               out.append(f"wk{w} BYE"); continue
           p = P.get(o[1:], {})
           rk, k3 = p.get("rk", {}).get(pos), p.get("rk3", {}).get(pos)
           if k3: r3.append(k3)
           out.append(f"wk{w} {o} {rk}/{k3}" + (" PO" if w >= 15 else ""))
       avg = sum(r3) / len(r3) if r3 else float("nan")
       print(f"{tm} {pos}: avg last-3 rk {avg:.1f} (32 = softest) | " + "; ".join(out))
   EOF
   ```
   Profiles cover QB/RB/WR/TE; K and DST are streamed by the week, so skip
   (c) for them.

d. **Injury context** (PLAYBOOK 4c): the official report (`official` in
   the sidecar), the newest `injuries.csv` row for the player AND the
   starter ahead of him, and a WebSearch (not WebFetch) for news since.
   State the role as "given who is hurt": who has to get hurt or return for
   it to change, one beneficiary or a split.

## 3. Rank, bid, drop

- **Season-long first** (PLAYBOOK 4d): RB/WR claims rank by role
  durability, then the rest-of-season schedule (playoff weeks included,
  byes counted), then this week's spot. A red spot this week is a reason to
  bench a claim, not to skip it. TE/K/DST are streamed purely by this
  week's board.
- **Bids price the field** (PLAYBOOK 4b2): $1 when the player is
  uncontested (low rostered %, no waiver-column interest, no team with a
  need at his spot). Go higher only for a second bidder you can point to,
  and then price the job, not the hope. Never double digits on QB/K/DST
  (4b); FAAB is for a starting RB. Apply PREFERENCES.md's waiver rules (e.g.
  the owner will not pay for a QB) and say which ones applied.
- **Name the drop for every claim**, from `roster.csv`, with its own
  usage/ROS line. Never propose dropping a PREFERENCES hold (e.g. Corum,
  Hampton) or anything on the owner's rejected list unless he raises it.
  An IR-slot player is not a drop candidate for an active slot.
- **Shared drops**: when two claims name the same drop, say so. Claims
  process in queue order; once the first lands the drop is gone and the
  later claim fails. That is right for a fallback ("fails harmlessly if #1
  lands") and wrong when the owner wants both - then name a second drop.
- **PASS list**: every named player not claimed, with a one-line reason
  (usage, schedule, or field).
- Quote the board grade for EVERY candidate first; a claim that disagrees
  with the board is **OVERRIDE** with one structural reason.

## 4. Log

- **The sheet**: one `who=claude` row - `decision`: `Week-<N> waiver sheet:
  1) RB <name> (<TEAM>) $<bid>, drop <name> - <one line>; 2) ...; PASS:
  ...`; `board`: the usage + spot + ROS line for each; `rationale`: the
  ranking logic and a **pre-registered grading rule** per claim (e.g. "hit
  if he holds 15+ carries a game through wk11; Johnson hit on a second game
  of 5+ targets or a start"); `outcome`/`verdict`: `pending`.
- **The owner's final queue**: a separate `who=user` row (`USER: final
  wk<N> queue - 1) ... $X; 2) ...`) logged as submitted; in your answer
  flag only where it differs from the sheet.
- When waivers process and the owner reports results, log them as a
  `who=user` row. Change `roster.csv` only for moves the owner states (use
  `python -m fantasy_football.cli roster add|drop` if it exists; otherwise
  edit the one line, keeping `player,pos,team,slot`).

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
