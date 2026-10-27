# Defense profiles: measured weekly notes + a durable scheme profile per defense

Build spec, written 2026-10-26. Decisions below were made by the repo owner;
do not reopen them. Everything not decided here is the builder's call, with
the repo's conventions (CLAUDE.md, PLAYBOOK.md) as the tie-breaker.

## Why

The Gemini scouting loop (`scout.py`, `scout.yml`) wrote one prose note per
defense per week. It is retired: its package percentages were invented and it
misplaced players. Checked against the 2026 snap counts, the wk3-6 notes put
Myles Garrett and Trent McDuffie on the wrong teams (both Rams), Sauce Gardner
(Colt), Riq Woolen (Eagle), Taron Johnson (Raider), Alontae Taylor (Titan),
Tyson Campbell and Jared Verse (Browns), Micah Parsons (Packer, IR). The code
and workflow are frozen on the `gemini-scout` branch; `scout.yml` on main has
no schedule. Nothing in this spec calls Gemini or any LLM.

## Decisions (owner, 2026-10-26)

1. Every number in a defense note is **computed from data**. No model.
2. `scout.yml`: schedule removed, manual dispatch kept, code left in place
   (done).
3. Each defense gets a **durable profile** shown in a new drop-down **above**
   the two existing ones on `#/defenses` ("by week", "weekly observation").
   Seed text for all 32 is in `defense_profiles.csv` (written by hand from the
   wk3-6 auto notes; those notes are archived at
   `archive/defense_notes_auto_2026_wk3-6.csv` and left in
   `defense_notes.csv` so the weekly-observation drop-down keeps showing
   them until measured notes supersede them, newest wins).
   When later weeks show a **trend (2-3 weeks)** away from the profile, the
   profile is adjusted. The weekly observation rows stay as they are.
4. A weekly note contains exactly: **base defense + % of snaps, second most
   common formation + %, share of plays with 5+ DBs on the field (nickel or
   heavier), blitz rate (5+ rushers) with average rushers, key defenders.**
5. "Coverage defense" = 5+ defensive backs on the field.
6. Key players = snap share first, then **contract and draft pedigree** as
   tie-breakers for who is a star.

## Data sources (all GitHub-hosted nflverse releases; verified reachable 2026-10-26)

| Need | Source | Status |
|---|---|---|
| Formation per play (`defense_personnel` e.g. `3 CB, 2 DE, 2 DT, 1 FS, 1 MLB, 1 OLB, 1 SS`, `defenders_in_box`, `number_of_pass_rushers`, `defense_coverage_type`, `defense_man_zone_type`) | `https://github.com/nflverse/nflverse-data/releases/download/pbp_participation/pbp_participation_{year}.csv` (also `.parquet`) | **2025 complete (22 weeks, 285 games). 2026 returns 404 today** in csv/parquet/rds. Build the parser against 2025, load 2026 when it appears, degrade when it is missing. |
| Pass rushers / blitzers / box count per play (`n_pass_rushers`, `n_blitzers`, `n_defense_box`, keyed by `nflverse_game_id` + `nflverse_play_id`) | `https://github.com/nflverse/nflverse-data/releases/download/ftn_charting/ftn_charting_{year}.csv` | 2026 present, 16,070 plays through week 6 (week 7 partial). Publishes a day or two after the games. |
| Which team is on defense for an FTN play, and pass/run | play-by-play parquet already used by `fetch_scheme_pbp` (`PBP_URL` in `ingest/nflverse.py`); join on `game_id` + `play_id`, use `defteam`, `pass`, `rush` | present |
| Snap share per defender (`defense_pct`, `position`, `team`, `week`, `game_type`) | `https://github.com/nflverse/nflverse-data/releases/download/snap_counts/snap_counts_{year}.csv` (already `fetch_snap_counts`) | 2026 present through week 7 |
| Contract (`apy`, `value`, `guaranteed`, `year_signed`, `is_active`), draft (`draft_year`, `draft_round`, `draft_overall`), `gsis_id`, `player` | `https://github.com/nflverse/nflverse-data/releases/download/contracts/historical_contracts.parquet` (csv is 404; pandas+pyarrow are in the `ingest` extra) | present; 53k rows, filter `is_active`, de-dup per player keeping highest `apy`; positions use `ED`/`IDL`/`LB`/`CB`/`S` |
| Official injury designations | already in `matchups.py` (`fetch_injuries`, `official_injuries`) | present |

Join contracts to snap rows by exact `player` name first, then by normalized
name (strip suffixes and punctuation); log unmatched names, never guess.
The participation file is the only source of true formation splits. The FTN
box count is a measured proxy for base-heavy vs light fronts and must be
used for the weekly note when participation is missing.

## Formation labels

From `defense_personnel`, count DL (`DE`, `DT`, `NT`, `DL`), LB (`MLB`,
`ILB`, `OLB`, `LB`), DB (`CB`, `FS`, `SS`, `S`, `DB`) and label
`<DL>-<LB>-<DB>` with a name: 4-3-4 / 3-4-4 / 4-4-3 / 5-2-4 = **base**;
4-2-5 / 3-3-5 / 2-4-5 = **nickel**; 4-1-6 / 3-2-6 / 2-3-6 = **dime**;
7+ DB = **quarter**; 6+ in the front with 3 DBs = **goal line**. Anything
with 11 players that does not fit gets the raw `D-L-B` string. Report the
label plus the digits ("4-2-5 nickel 61%"). "5+ DB share" is the sum of
nickel, dime, quarter and any other 5+ DB personnel.

## Deliverables

### A. `defense_profiles.csv` reader + payload + page drop-down

- File columns `team,date,profile`. Rows append; newest per team wins
  (same rule as `defense_notes.csv`). Reader next to `read_defense_notes`
  in `matchups.py`, same hardening (team code <= 3 chars, text cap, row cap).
- Payload: `FF_DVP.scheme[TEAM] = {"d": date, "t": text, "m": <measured block>}`.
  `m` is the season-to-date measured block (section C) with `last3` inside.
- `#/defenses`: a `<details class='dvp-prof'>` placed **before** the "by week"
  details, summary `defensive profile (season through wk N, measured; text
  seeded YYYY-MM-DD)`. Inside: the measured block as a short table or
  inline chips (base formation + %, second formation + %, 5+ DB %, blitz %
  and avg rushers, 7+ box % if participation is missing), then the key
  defenders line, then the profile text. When a trend flag is set (section
  D) show a `▲ profile shift` chip in the summary. Page leads stay one or two
  sentences; the legend goes in the `.foot` paragraph (extend the existing
  one). The app must degrade when `dvp.js` predates the field.

### B. Measured weekly note -> `defense_notes.csv`

- New CLI `defense-notes-measured --year Y --week W [--notes defense_notes.csv]`
  (name is the builder's choice; keep it next to the `dvp` command).
  For each defense that played week W, append one row
  `TEAM,YYYY-MM-DD,"WkW measured: base 4-2-5 nickel 61% · 2nd 4-3 base 22% ·
  5+ DB 74% · blitz 24% (avg 4.3 rushers, 7+ box 31%) · key: Roquan Smith LB
  100%, Kyle Hamilton S 97% $, Nate Wiggins CB 99% R1 ..."`. Whole numbers
  only. Prefix `WkW measured:` so `fmtNote` renders it (no `Fantasy read:`
  sentence; the app already copes without one) and so a reader knows it is
  not prose. Idempotent: skip a team that already has a `WkW measured:` row
  for that week unless `--force`.
- Formation fields come from participation. When the 2026 participation
  file is missing, write the FTN box-count split instead and label it:
  `base/2nd: n/a (no participation file) · 7+ box 31% · light box (<=6)
  52%`, keep 5+ DB as `n/a`, and still write blitz, rushers and key players.
  Do not estimate a formation split from snap counts; the existing
  `package_rates` DB-per-snap average is already on the identity line.
- Blitz rate = share of pass plays (pbp `pass == 1`, sacks and scrambles
  included) with `n_pass_rushers >= 5`; also report mean `n_pass_rushers`.
  Use `n_blitzers` only as a cross-check in tests, not in the note.
- Key defenders (also used by section C): candidates are defenders with
  >= 50% average `defense_pct` over the games they played and >= 2 games
  (or every game when only one is played). Score = snap share, plus a star
  bonus: `apy >= 15.0` ($M) or `draft_round == 1` with `draft_year >=
  season - 4`, or `draft_overall <= 10` any year. Take the top 6, at least
  one from each of DL/EDGE, LB, DB when available. Render `Name POS
  snap% [$][R1]`, and `OUT` when the official report lists the player Out
  or Doubtful for the coming week (same source as `official_flags`).
- Hook it into `dvp.yml` after the DvP build on the Tuesday and Wednesday
  runs (the Wednesday run is the one that sees the full snap counts and the
  FTN file; Tuesday may be a partial week, so run it with `--force` on
  Wednesday so the row is refreshed, or only run it Wednesday). Commit
  `defense_notes.csv` with `docs/dvp.js`. The CSV is also read by the
  retired `scout-run`; do not break that reader.

### C. Season measured block (for the profile drop-down)

Same fields as the weekly note, computed season-to-date and for the last
three games: `{"base": ["4-2-5 nickel", 61], "second": ["4-3 base", 22],
"db5": 74, "blitz": 24, "rushers": 4.3, "box7": 31, "last3": {...same
keys...}, "key": [{"n": ..., "pos": ..., "snap": 97, "star": "$", "out":
false}, ...], "src": "participation" | "ftn-only"}`. Attach as
`FF_DVP.scheme[TEAM].m`. Compute it inside `write_dvp_js` so one `dvp` run
produces everything; cache the downloaded files per run the way
`fetch_snap_counts` is used today.

### D. Trend flag and profile adjustment

- A **shift** is: for two or more consecutive recent weeks, the weekly
  base formation differs from the season base, OR the weekly 5+ DB share or
  blitz rate differs from the season figure by 15 points or more in the same
  direction. With participation missing, use 7+ box share and blitz rate.
- Set `FF_DVP.scheme[TEAM].shift = {"since": week, "what": "nickel 5+DB 58 ->
  81", ...}`; the page shows the chip. Nothing auto-rewrites the profile
  text: the Tuesday Routine (CLAUDE.md "Injury sweep") appends a new
  `defense_profiles.csv` row for flagged teams, and the CLI prints the
  flagged teams at the end of the measured run so the Actions job summary
  lists them. Add one line to PLAYBOOK.md: a matchup recommendation quotes
  the measured block, not the seed text, when they disagree.

### E. Tests (offline, fixtures only)

- Formation labeler: table of `defense_personnel` strings -> label.
- Participation + pbp + FTN fixtures for one fake game -> expected base %,
  second %, 5+ DB %, blitz %, mean rushers, 7+ box %.
- Missing participation -> `src == "ftn-only"`, `n/a` fields, note still
  written.
- Key-defender scoring with a contracts fixture: a $20M corner at 80%
  outranks a $1M corner at 85%; a 2026 first-rounder gets `R1`; an Out
  designation renders `OUT`; unmatched contract names do not crash.
- Note idempotence (`--force`) and the newest-wins reader for
  `defense_profiles.csv`.
- Shift detection: two weeks of +15 blitz -> flag; one week -> no flag.
- `pytest` passes; keep the existing suites green (`test_scout.py` stays,
  the module is frozen not deleted).

### F. Docs

- CLAUDE.md: replace the retired-loop paragraph pointer with a short
  description of the measured layer (what runs when, the files, the
  `src` fallback), and update the **App pages** list for the new drop-down.
- `.claude/skills/defense-notes/SKILL.md`: the Gemini loop is retired;
  the skill now covers (1) reading the measured note, (2) appending a
  `defense_profiles.csv` row when the shift chip is up, (3) the hand-paste
  path stays as a manual override (newest wins). Keep it short.
- `prompts/README.md`: one dated changelog line that the loop is retired
  and why, pointing at the `gemini-scout` branch.

## Repo facts the builder needs

- Offline tests, `pythonpath = ["src"]`, never a network call in tests.
- `fmtNote(date, text)` in `docs/app.js` splits on `Fantasy read:`; the
  weekly card shows `V.notes[TEAM][0]` (newest). `read_defense_notes` keeps
  the newest three per team, cap 900 chars.
- `cap_note` (900) applies to every note; a measured note with six key
  players fits in ~350 chars.
- All dollars/percentages in this toolkit render as whole numbers.
- `dvp.yml` already loads the current and previous season and runs
  `cli dvp --out docs/dvp.js`, then bumps `dvp.js?v=` in `docs/index.html`.
- The web sandbox can reach GitHub-hosted nflverse files (verified for every
  URL above); a local DB load hit a 403 on one non-GitHub URL, so the
  builder should not rely on `load-season` in the sandbox and should work
  from the release files + fixtures.
- Whole-pool key: 2026 snap counts list positions as `DE/DT/NT/DL`,
  `LB/ILB/OLB/MLB`, `CB/S/FS/SS/DB`.

## Done when

`pytest` green; `python -m fantasy_football.cli dvp --out docs/dvp.js` in
Actions produces `FF_DVP.scheme` for all 32 teams with `src` set; the
measured CLI appends 32 rows for a completed week; `#/defenses` shows the
new drop-down above "by week" with the seed text and the measured block, and
still renders with an old `dvp.js`; CLAUDE.md, the skill and the PLAYBOOK
line are updated; the work is pushed to the `defense-profiles` branch with a
PR against `main` describing the fallback behaviour while the 2026
participation file is missing.
