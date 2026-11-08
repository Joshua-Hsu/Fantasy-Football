# Build 2: `PREFERENCES.md` - the owner's standing calls, in the repo

Written 2026-11-08.

## Why
The owner's standing rules live only in one chat session. A fresh session
must read them before any recommendation.

## Decisions (owner)
1. New file `PREFERENCES.md` at the repo root. Plain Markdown, one bullet
   per standing call, each with the date it was stated and, where one
   exists, the `decisions.csv` row date it came from. Group into sections:
   Roster holds, Waiver rules, Lineup rules, Reporting preferences, Things
   the owner has rejected.
2. Seed it from (a) the list below, (b) every `who=user` row in
   `decisions.csv` (read them all; keep what is a standing rule, skip
   one-week lineup calls), and (c) PLAYBOOK.md rules that originated as the
   owner's calls (they say "user call" in the text).
3. The file is append-only in spirit: a later call that reverses an earlier
   one is added with the date and the earlier bullet is marked "superseded
   <date>", never deleted.
4. CLAUDE.md: add one paragraph under "In-season decision discipline"
   saying PREFERENCES.md is read before PLAYBOOK.md for any roster
   recommendation and is updated whenever the owner states a standing rule.
   PLAYBOOK.md: add "0. Read PREFERENCES.md" above the checklist.

## Seed list (from the 2026 season chat, dates approximate to the week)
Roster holds
- Hold Blake Corum ("I'm not dropping Corum", wk5). Hold Hampton "until the
  wheels fall off" (wk8). Held Jacobs through the exempt list (wk2-7).
  Keep Okonkwo at TE until he proves himself either way (wk8); Freiermuth
  was dropped wk9. Roschon Johnson re-added wk9 as Swift/Monangai cover.
Waiver rules
- Season-long first: rank adds by role durability and rest-of-season
  schedule, this week second (wk7, now PLAYBOOK 4d). Devalue last season's
  data from week 6 on (wk7, PLAYBOOK 2c). A bid prices the field, not the
  player: $1 when uncontested (wk9, PLAYBOOK 4b2). FAAB is for a starting
  RB; never double digits on QB/K/DST. Will not pay for a QB (passed Mahomes
  at $4, wk9). Likes Antonio Williams and Keon Coleman (wk7). Prefers to
  "take a shot" on upside backs over handcuff stashes (wk8).
Lineup rules
- Believes in rivalries as a tiebreak (wk5; the board does not - log it as
  the owner's reason). Kamara over Corum as the RB fallback when Rhamondre
  is out (wk5). When the owner states a preference phrased as a question
  ("is X not a play?"), run the full case for X before answering (wk5
  Evans miss).
Reporting preferences
- No narration of routine ship steps; speak mid-task only with information.
- Defense reads come from the measured profiles, not weekly prose (wk7).
- Charts: no Unicode sparklines; fixed-width SVG bars + a by-week table.
- Roster file changes only when the owner states them; every Tuesday sweep
  reads the roster back first (wk7 correction).
Rejected
- A $12 Shipley bid (wk5). Dropping Corum. Streaming the Rams DST out for
  marginal upgrades (held them repeatedly). Mason Taylor over Okonkwo (wk8).

## Deliverables
- `PREFERENCES.md`, CLAUDE.md paragraph, PLAYBOOK.md step 0.
- A test is not required; `pytest` must still pass.
