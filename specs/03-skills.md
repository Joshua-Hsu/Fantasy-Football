# Build 3: skills `/lineup`, `/waivers`, `/sweep`

Written 2026-11-08. Depends on builds 1 and 2 by NAME only: reference
`python -m fantasy_football.cli lineup` and `PREFERENCES.md` as given; do not
wait for them.

## Why
The weekly procedures exist as a Routine prompt and as habits in one session.
Each must be a committed skill a fresh session can run to completion.

## Decisions (owner)
1. Three skills under `.claude/skills/<name>/SKILL.md` (frontmatter `name`,
   `description` with the trigger phrases, then the procedure). Look at the
   existing `.claude/skills/defense-notes/SKILL.md` for the house format.
2. Every skill starts with the same **setup block**: fetch main and
   `git checkout -B claude/<branch> origin/main`; verify the DB (`cli info`,
   row counts) and rebuild it if empty using the teams-URL workaround in
   `specs/01-lineup-command.md`; `pip install -e ".[dev,ingest,export,web]"`
   if imports fail; read `PREFERENCES.md`, `PLAYBOOK.md`, `roster.csv`
   (read the roster back to the owner before any recommendation); rebuild
   `docs/dvp.js` with `cli dvp --year <this> --baseline-year <last>`.
3. Every skill ends with the same **ship block**: append rows (never edit
   old ones) to `decisions.csv` / `injuries.csv` with `csv.writer(...,
   lineterminator="\n")`, bump the `dvp.js?v=` cache-buster in
   `docs/index.html` to the minute, `pytest`, commit with the attribution
   lines the session was given, push with `--force-with-lease` and a
   2/4/8/16s retry on server errors, open a PR to main, squash-merge it,
   realign the branch to main, verify with `git show origin/main:decisions.csv | tail -2`.
   On a merge conflict in generated files (`docs/dvp.js`, `docs/data.js`,
   `docs/index.html`): rebase, take theirs, regenerate, continue.
4. `/lineup`: run `cli lineup`; apply the injury sweep for the roster
   (WebSearch, not WebFetch); state the lineup slot by slot with the reasons
   the command printed and the owner's PREFERENCES applied (e.g. Corum
   hold); name every questionable starter and the swap; log a
   "FINAL week-N lineup" row with `who=claude`. If the owner has given a
   lineup, log theirs with `who=user` and only flag differences.
5. `/waivers`: input is the owner's wire (screenshots or names). For every
   named player: `cli usage`, this week's spot from the sidecar (season /
   last-3 / trend / implied) and the rest-of-season schedule on the
   profiles (weeks ahead through 16, average last-3 rank, playoff weeks
   15-16 named). Rank season-long first (PLAYBOOK 4d), set bids by the
   field rule (4b2), name the drop for each claim and warn when claims share
   a drop. Log the sheet; log the owner's final queue as `who=user`.
6. `/sweep`: the Tuesday procedure, copied from the Routine prompt in
   CLAUDE.md's "Injury sweep" paragraph and made explicit: grade every
   pending `decisions.csv` row from the box scores (`cli usage`; abstentions
   are neutral, never hits), injury sweep (roster + opponents first, then
   the wire), append `injuries.csv` rows, handle `FF_DVP.scheme[*].shift`
   flags by appending a `defense_profiles.csv` row for each flagged team,
   waiver sheet + tentative lineup rows, then the ship block, then the
   report (grades table, injuries with source links, sheet with bids and
   drops, tentative lineup).
7. Each SKILL.md carries a "Known traps" list: the TEAMS_URL redirect; the
   stale remote ref after a container reset (`git fetch origin <branch>`
   before `--force-with-lease`); the DST implied-total column mix-up
   (`vegas[opp].it` is the OPPONENT's total - the Ravens error, wk9); the
   one-game rule and its exception (a trend, e.g. Washington wk9); the
   Pickens/validate_next lesson (names in prose are checked against
   `Player.current_team`); `roster.csv` is changed only on the owner's word.
8. Update CLAUDE.md "In-season decision discipline" to name the three
   skills and say the Tuesday Routine is a backstop that runs `/sweep`.

## Deliverables
- The three SKILL.md files; CLAUDE.md paragraph. `pytest` still green.
- Dry-run each skill's commands once in the sandbox (setup block through
  `cli lineup` / `cli usage`) and note in the PR which steps you verified.
