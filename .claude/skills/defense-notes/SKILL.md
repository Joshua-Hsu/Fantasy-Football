---
name: defense-notes
description: Weekly defensive scouting loop for the Matchups page - generate the two-defense Gemini questions per game, verify each paste against the box scores, file capped notes to defense_notes.csv, regenerate docs/dvp.js and ship. Use when the user says "start week N defensive notes", pastes a Gemini scouting answer, or asks for the next scouting prompt.
---

# Weekly defense notes

`defense_notes.csv` (`team,date,note`) carries one scouting note per defense
per week. `#/dvp` shows the three newest per team, capped at 900 chars.
Notes are **appended, never edited**: a corrected note supersedes the old one.

## The loop (Tuesday/Wednesday after the box scores load)

1. **Fact sheet first.** Build the per-defense numbers for the week so every
   paste has something to be checked against:
   `python -m fantasy_football.cli scout-verify --team ALL` is not a thing -
   use the DB directly (points allowed by position that week, season ranks,
   DB/LB per snap, pressure rate, opponent plays, personnel flags) as in the
   week-2 fact sheet, and list the defenses the user's players face next week
   first (roster = My Team keys on `#/dvp`).
2. **Hand the user one prompt per game**, both defenses in one question:
   `python -m fantasy_football.cli scout-prompts --game DEN-JAX`.
   The wording matters. A terse "scout X for fantasy" prompt came back as one
   thin paragraph; the fan-style question in `scout._PROMPT` came back with
   scheme, personnel percentages, injuries and a fantasy translation for both
   teams. Always give the NEXT prompt at the end of every reply, in a fenced
   block, ready to copy, in EXACTLY that format - the user asked that the
   copy-paste prompt not carry extra stats (the box lines are appended only
   on the Gemini API path, `--lines` opt-in).
3. **Verify every paste before filing.** Run
   `python -m fantasy_football.cli scout-verify --team DEN,JAX` and check each
   named line (carries, targets, yards, TDs). Where the paste's *story*
   contradicts the numbers (e.g. "erased underneath" when the TE went 12-140,
   "shut down the run" when the RB scored twice), file the number and say so
   in the note ("verified" / "the data disagrees"). Never file a claim that
   the box score contradicts without the correction next to it.
4. **File the note**: `team,YYYY-MM-DD,"..."` - scheme + pressure design,
   what changed and why, defensive injuries with who filled in, what the
   offense did (with the verified lines), season ranks by position, the
   fantasy translation, and a line for the next opponent naming the user's
   own players if they are in it. Keep it <= 900 characters (`cap_note`).
5. **Regenerate and ship**: `cli dvp --year <yr> --baseline-year <yr-1> --out
   docs/dvp.js`, bump `dvp.js?v=` in `docs/index.html`, commit, push, keep ONE
   batch PR open for the week's notes and merge it when the batch is done
   (or immediately if a note changes a lineup call before games).
6. **Restate any lineup call the note moves** (PLAYBOOK rule 3 "update, don't
   defend"): say whether it reverses the logged decision, and log it.

## Automated path

With a `GEMINI_API_KEY` repo secret, `.github/workflows/scout.yml` runs
`cli scout-run` on Tuesdays after the DvP refresh: one grounded Gemini call
per game, sections split per team, notes prefixed `Wk<N> auto (Gemini,
unverified):`. Those still need step 3 before they are trusted for a lineup
call; the user's pasted notes supersede them (newest wins).

Without the key, the fallback is the session's own WebSearch sweep for the
defenses the user's players face, filed with the same verification.
