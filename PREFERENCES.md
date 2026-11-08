# Owner preferences

The owner's standing calls. Read this before `PLAYBOOK.md` for any roster
recommendation (add, drop, start, sit, bid, trade). The playbook is the
checklist; this file is what the owner has already decided.

How to maintain it:

- One bullet per standing call. The date it was stated goes in parentheses;
  where the call is logged, `decisions.csv <date>` names the row it came from.
  Seed items without an exact date are approximate to the week (`wkN, ~date`).
- Append-only in spirit. When the owner reverses a call, add the new bullet
  with its date and mark the earlier one `superseded <date>`. Never delete.
- Add a bullet whenever the owner states a standing rule. One-week lineup
  calls go in `decisions.csv`, not here.
- When a recommendation goes against a bullet here, say so and label it
  OVERRIDE, the same way a call against the board is labeled.

## Roster holds

- Hold Blake Corum. "I'm not dropping Corum." The owner declined the Corum
  drop for an RB bid and again for a Shipley bid. (wk4-5, decisions.csv
  2026-09-29, 2026-10-06)
- Hold Omarion Hampton "until the wheels fall off". (wk8, ~2026-10-27)
- Josh Jacobs was held through the commissioner-exempt list and started on
  reinstatement. Exempt (`EXE`) is not a reason to drop. (wk2-7, decisions.csv
  2026-10-20)
- Drake Maye is the QB. The owner believes in him and declined the board's QB
  streams; a matchup stream does not replace him. (wk3-4, decisions.csv
  2026-09-22, 2026-09-30)
- Alvin Kamara is a starting NFL RB with a role (9+ touches in a split) and is
  not a drop for a QB stream. (wk4, decisions.csv 2026-09-30)
- Keep Chig Okonkwo at TE until he proves himself either way; no Mason Taylor
  claim. (wk8, decisions.csv 2026-10-27)
- Hold Pat Freiermuth until Okonkwo proves himself. (wk8, decisions.csv
  2026-10-27) - superseded 2026-11-03: Freiermuth dropped wk9.
- Roschon Johnson re-added (for Jaydon Blue) as Swift/Monangai cover in
  Chicago. (wk9, decisions.csv 2026-11-03)
- The Rams DST is held; see Rejected. (wk5-9, decisions.csv 2026-10-06)

## Waiver rules

- Season-long first, this week second: rank RB/WR adds by role durability and
  the rest-of-season schedule (playoffs wk15-16, byes counted), then by this
  Sunday's spot. A red spot this week is a reason to bench a claim, not to
  skip it. Only TE/K/DST are streamed by the week. (wk7, ~2026-10-20; PLAYBOOK
  4d)
- Devalue last season's data from week 6 on: the current-season rank decides,
  last season is only a tiebreaker. (wk7, ~2026-10-20; PLAYBOOK 2c)
- A bid prices the field, not the player: $1 when uncontested (low rostered %,
  nobody naming him, no team with a need at his spot). Go higher only for a
  second bidder you can point to. (wk9, ~2026-11-04; PLAYBOOK 4b2)
- FAAB is for a starting RB. Never double digits on QB, K or DST. (wk3,
  ~2026-09-22; PLAYBOOK 4b - the owner called the drafted $22 Bryce Young bid
  poor process)
- Will not pay for a QB. Passed Mahomes, who went for $4; a bye week gets a $0
  streamer. (wk9, decisions.csv 2026-11-04)
- Never spend a roster spot that has RB volume on a QB stream. Streams come
  from the WR6/K/DST end of the bench or not at all. (wk4, decisions.csv
  2026-09-30)
- When the queue at a position is deep (TE), losing a claim costs less, so
  bid lower. (wk2, decisions.csv 2026-09-16)
- Prefers to "take a shot" on upside backs over handcuff stashes. With $0 free
  agents, cycling the open spot through RB lottery tickets is fine, even ahead
  of a TE the roster would not start. (wk5-8, decisions.csv 2026-10-10;
  ~2026-10-27)
- Leans toward proven backs: a player who has done it before with a real
  touch count (Kamara's debut) earns a higher bid than a one-game sample.
  (wk3, decisions.csv 2026-09-22)
- Players the owner likes: Antonio Williams (WAS WR) and Keon Coleman (BUF WR).
  (wk5-8, decisions.csv 2026-10-10, 2026-10-27; ~2026-10-20)
- Deshaun Watson (CLE) is the preferred QB add if one is ever made, for his
  rushing, timed to a green matchup. (wk4, decisions.csv 2026-09-30)

## Lineup rules

- WR slots are a weekly pick of 3 from the floors (Rice/Adams/Higgins/Evans at
  the time) by weekly pop, not a fixed order, until a Jameson-type posts a
  smash or a lock scheme matchup. (wk3, decisions.csv 2026-09-22)
- Kamara over Corum as the RB fallback when Rhamondre is out; Corum only if
  Kamara is out too. Logged as an override of the board. (wk5, decisions.csv
  2026-10-10)
- Believes in rivalries as a tiebreak. The board does not, so log it as the
  owner's reason when it decides a call. (wk5, ~2026-10-10)
- When the owner states a preference as a question ("is X not a play?"), run
  the full case for X before answering. (wk5, ~2026-10-10; PLAYBOOK graded
  misses, Evans @SEA)

## Reporting preferences

- No narration of routine ship steps. Speak mid-task only with information.
  (2026 season)
- Defense reads come from the measured profiles (`#/defenses`, season rank,
  last-3 rank, measured block), not weekly prose; a note is color, never the
  grade. (wk7, ~2026-10-20; PLAYBOOK 2a)
- Charts: no Unicode sparklines. Use fixed-width SVG bars plus a by-week table.
  (2026 season)
- `roster.csv` changes only when the owner states a change. Every Tuesday
  sweep reads the roster back first. (wk7 correction, ~2026-10-20)

## Things the owner has rejected

- A $12 Shipley bid. (wk5, decisions.csv 2026-10-06)
- Dropping Corum. (wk4-5, decisions.csv 2026-09-29, 2026-10-06)
- Streaming the Rams DST out for marginal upgrades; held them repeatedly,
  including the worst DST spot on the wk5 board. (wk5-9, decisions.csv
  2026-10-06; ~2026-11-03)
- Mason Taylor over Okonkwo. (wk8, decisions.csv 2026-10-27)
- Stroud-for-Kamara as a $1 QB stream. (wk4, decisions.csv 2026-09-30)
- The board's QB streams over Maye (Lock wk3; Brissett/Shough wk4).
  (wk3-4, decisions.csv 2026-09-22, 2026-09-30)
