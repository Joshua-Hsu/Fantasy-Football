# State files

The hand-kept CSVs a fresh session relies on. `python -m fantasy_football.cli
check-state` validates all four (errors exit 1; warnings print and pass). It
runs in the test suite and in `dvp.yml` before the build, so a broken row
fails loudly instead of being dropped from the app.

File handling: every file is UTF-8 with a header row. Readers open with
`newline=""`; writers use `csv.writer(..., lineterminator="\n")` with
`QUOTE_MINIMAL` and re-emit untouched rows byte-for-byte (`state.CsvDoc`), so
an edit changes only the row it touches. Quote any field that contains a
comma: an unquoted comma shifts the row to 10+ columns and fails the check.

## roster.csv

The user's current roster. Change it **only** with `cli roster add NAME
[--slot S] [--drop NAME]`, `cli roster drop NAME` or `cli roster move NAME
--slot S`. Each resolves the name against the DB and appends a `who=user` row
to `decisions.csv` (`Roster: added X / dropped Y`). `cli roster show` prints it
grouped by slot.

| column | type | rule | example |
|---|---|---|---|
| player | text | `Player.full_name`; must resolve to one DB player (case, punctuation and Jr./Sr./II-IV ignored). DST rows hold the team code | `Rashee Rice`, `LA` |
| pos | enum | `QB` `RB` `WR` `TE` `K` `DST` | `WR` |
| team | code | one of the 32 nflverse codes (`LA` = Rams, `LV`, `WAS`) | `KC` |
| slot | text | non-empty; the leading word is the slot (`QB`, `RB1`, `WR2`, `TE`, `FLEX`, `K`, `DST`, `bench`, `IR`), and anything after it is a note | `bench (claimed wk9, $3)` |

Warnings: more than 15 non-IR players; non-IR players that can't fill the 9
starters (1 QB, 2 RB, 2 WR, 1 TE, 1 FLEX from RB/WR/TE, 1 K, 1 DST); a DB
position or team that differs from the row.

## decisions.csv

The pre-registered decision log (`#/log`). Append with `cli log add --week N
--who claude|user --decision ... --board ... --override ... --rationale ...`,
which writes `outcome` empty and `verdict=pending`. Grade with `cli log grade
--row I --outcome ... --verdict hit|miss|neutral` (it refuses a graded row
unless you pass `--force`). `cli log list [--pending]` shows the row numbers,
where 1 is the first row after the header.

| column | type | rule | example |
|---|---|---|---|
| date | date | ISO `YYYY-MM-DD`, the day the call was made | `2026-11-03` |
| week | int | NFL week; `0` = preseason | `9` |
| decision | text | non-empty; the call itself | `Start Rice at FLEX over Corum` |
| who | enum | `claude` or `user` | `claude` |
| board | text | the matchup board's grade for every candidate | `Rice 14.1 (+8%) vs Corum 9.0` |
| override | text | starts with `yes` or `no` (case-insensitive). After a yes, give the structural reason | `YES (structural: Evans out)` |
| rationale | text | written before the games | `Board order; usage agrees` |
| outcome | text | empty until graded | `Rice 18.2 on 9 targets` |
| verdict | enum | `pending` `hit` `miss` `neutral` | `hit` |

## injuries.csv

The weekly injury sweep (`#/injuries`). Append rows and never edit them. The
newest row per player wins.

| column | type | rule | example |
|---|---|---|---|
| date | date | ISO `YYYY-MM-DD` | `2026-11-03` |
| player | text | non-empty | `Mike Evans` |
| team | code | team code; may be blank | `SF` |
| pos | text | free (defenders too: `EDGE`, `CB`, `S`) | `WR` |
| status | text | `OUT`, `IR`, `EXEMPT`, `questionable`, ... | `OUT` |
| injury | text | what and how | `"ribs (hit wk8, DNP Wed)"` |
| timeline | text | expected return | `out wk9; re-check Friday` |
| replacement | text | who absorbs the work | `Jennings WR1 targets` |
| note | text | the scheme read | `SF leans on Kittle` |

Rule: exactly 9 columns per row.

## defense_profiles.csv

One durable paragraph per defense (`#/defenses`). Append a new row when the
profile-shift chip fires and never edit old rows. The newest row per team
wins. The file uses CRLF line endings, and edits keep them.

| column | type | rule | example |
|---|---|---|---|
| team | code | team code of 3 characters or fewer, from the 32 nflverse codes | `ARI` |
| date | date | ISO `YYYY-MM-DD` | `2026-10-26` |
| profile | text | non-empty | `"Nickel-based, two-high shell..."` |

Rule: exactly 3 columns per row.
