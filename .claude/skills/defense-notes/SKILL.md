---
name: defense-notes
description: Weekly defense notes for the Defenses/Matchups pages - read the computed "WkN measured:" note and the profile drop-down, append a defense_profiles.csv row when a defense shows the profile-shift chip, and file a hand-pasted note as a manual override. Use when the user says "start week N defensive notes", asks what a defense's scheme is, pastes a scouting answer, or a measured run flags a profile shift.
---

# Defense notes (measured)

The Gemini scouting loop is **retired** (2026-10-26): its package and blitz
percentages were invented and it put players on the wrong teams. Code frozen
on the `gemini-scout` branch; do not revive it or call any LLM for numbers.
Spec: `specs/defense-profiles.md`. Every number now comes from nflverse files
(`defense_measured.py`).

## 1. Read the measured note

- `defense_notes.csv` gets one `WkN measured:` row per defense from the
  Wednesday `dvp.yml` run (`cli defense-notes-measured --force`). Rerun by
  hand: `python -m fantasy_football.cli defense-notes-measured --year <yr>
  [--week N] [--force]`.
- Each note is the computed game story: opponent + score, **Faced** (who
  produced against the defense), **Personnel** (outs, returns, snap-share
  moves from the snap counts), **Scheme** (blitz, rushers, box; formation
  once participation exists), **Fantasy read** (points allowed by position
  vs the defense's own average) and the next opponent with season ranks.
  Every number is from data; it needs the DB loaded (`load-season`) or it
  falls back to the bare measured line.
- `base/2nd: n/a (no participation file)` means the season's participation
  file is not out yet (`src: "ftn-only"`); quote the box split and blitz
  rate, never estimate a formation.
- For a matchup call quote the `#/defenses` profile drop-down's **Season /
  Last 3** numbers (PLAYBOOK 2a), not the seed text and not one week's note.

## 2. Profile shift -> append a profile row

A shift is measured against the team's CURRENT profile: the games before
the newest `defense_profiles.csv` row's date are the baseline, and 2+
straight games since then sat 15+ points off it (or on another base
formation). When the run prints `Profile shift flagged` (also in the Actions
job summary) or a card shows `▲ profile shift`, read the shift line, the
Season vs Last 3 numbers and the measured notes since the profile date, then
**append** (never edit) a row to
`defense_profiles.csv`: `TEAM,YYYY-MM-DD,"..."`, <= 900 chars, describing
the new habit in measured terms (formation, 5+ DB, blitz, box, who plays).
Newest row per team wins, and its date resets the shift baseline, so the
chip clears. Regenerate `docs/dvp.js` with `cli dvp` (or let
the next Action do it) and ship via PR.

## 3. Hand-paste override

A note the user pastes (or a verified WebSearch finding) still goes in
`defense_notes.csv` as `TEAM,YYYY-MM-DD,"..."` and supersedes the measured
row by date (newest wins). Check every named stat against the box score
first (`cli scout-verify --team DEN,JAX` still works); file the number and
the correction where the story disagrees. Keep it <= 900 characters.
