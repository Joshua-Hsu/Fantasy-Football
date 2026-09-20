# In-season decision playbook

Read this before recommending ANY roster move (add, drop, start, sit,
trade target, FAAB bid). It exists because of graded misses, listed at the
bottom. The rules are mechanical on purpose: the failures were not bad
luck, they were skipped steps.

## The pre-recommendation checklist (all five, every time)

1. **Current-season usage for every candidate, from the database.**
   `python -m fantasy_football.cli usage --player "<name>"` prints the
   week-by-week targets/carries/yards/points for this season next to last
   season's averages. Last-season PPG is NOT a role. A player with 2
   targets in a game is not a "starter with upside" until the usage says so.
   *(Loveland: 2 targets, 0 catches, 0.0 in wk1 - in the DB when he was
   recommended. Ferguson: 2 targets in wk1 - in the DB when he was bid on.)*
2. **The matchup board's grade for every candidate.** `#/dvp` My Team
   Proj, or the defense's rank/badges/notes for the opponent. Quote it.
   If the recommendation disagrees with the board, write **OVERRIDE** and
   exactly one structural reason (injury, depth chart, scheme note). No
   structural reason = the board wins.
2b. **An average is not a ceiling.** Quote the weekly distribution
   (`cli usage` prints the best games), never describe a player's mean as
   his cap. *(Schultz: called a "hard ~8-point ceiling"; 8.0 was his
   average - he had four 12+ games in 2025 and scored 20 the next week.)*
2c. **The agreement rule applies to prose, not just row colors.** When
   this season's rank and last season's disagree hard, say "no verdict",
   do not pick the side that makes the better story. *(CIN 5th vs TE in
   wk1, 32nd in 2025 - "trust the week-1 number" was a coin flip dressed
   up as analysis.)*
3. **One game is not a role.** A usage claim needs two games or a
   structural cause. This applies symmetrically: one bad game does not kill
   a role either. *(Gesicki's 7 targets were one game, in a defined funnel;
   the note said so; the recommendation ignored the note.)*
4. **League format.** 12 teams, 15-man rosters, $100 FAAB. Replacement
   level is high and bench spots are scarce: for streaming positions (TE,
   K, DST) either pay for a clear starter or stream purely by the matchup
   board at $0-1. Never bid $3+ on a narrative.
5. **Log it before the games.** Add the row to `decisions.csv` with the
   board grade and rationale, then grade it after. Unlogged calls don't
   count; overrides are tracked separately on `#/log`.

## What counts as a gradable call

A logged decision is only eligible for **hit/miss** if it was complete
enough to be wrong: the player to add, the player to drop, the bid amount,
or the lineup slot. "Don't chase him" with no drop named is not a call - it
is logged as **neutral** and never counts toward the hit rate. *(The
Mahomes "pass" was first graded a hit; it was regraded neutral because no
drop or bid was ever specified, so it could never have missed.)*

## When new evidence arrives after a recommendation

Restate the decision from zero. Say plainly if it reverses the earlier
call. There is no consistency premium - defending a Tuesday call against a
Wednesday board is how the Ferguson TD was given away.

## Things that were assumed and were wrong

- Player teams change. Read `team` from `data.js` / the DB, never memory
  (Mike Evans is SF; Etienne is NO; DJ Moore is BUF).
- Roster status codes: commissioner-exempt is `EXE` and stays active.
- The web IS reachable from the harness (WebSearch/WebFetch run outside the
  container's network policy). Test a tool before declaring it unavailable.
- In-season, "latest season" is a one-week sample; builds pin to the last
  completed season (`_latest_completed_year`).

## Graded misses this list is built from

| Date | Call | What was skipped | Cost |
|---|---|---|---|
| 2026-08-26 | Claim Ferguson $2 | Step 1 (usage): wk10+ fade was in the data; user flagged it | $2, a roster churn |
| 2026-09-16 | Drop Ferguson for Gesicki $4 | Steps 2+3: board had Ferguson vWAS as the best TE matchup; 7 targets was one game in a funnel | $4 and a TD in a must-win week |
| 2026-09-15 | Loveland named a trade target | Step 1 (usage): 2 tgt / 0 rec / 0.0 in wk1, in the DB | credibility; 0-point weeks 1-2 |
| 2026-09-16 | Talked out of queuing Schultz vCIN ("floor only", "bad spot") | Steps 2b+2c: average called a ceiling; sided with a 1-week rank against a 32nd '25 rank | Schultz 20, Gesicki 0 |
