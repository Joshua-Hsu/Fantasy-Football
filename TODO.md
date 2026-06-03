# TODO

## Coaching data (OC / play-caller)

Head coaches load automatically from nflverse, but **offensive coordinator** and
**play-caller** are not in nflverse and currently show **TBD** on the cards and
the draft board's Coaching sheet.

To fill them:

1. `python -m fantasy_football.cli coaching-template --out coaching.csv`
   (writes all 32 teams with head coaches pre-filled)
2. Fill the `offensive_coordinator` and `play_caller` columns.
3. `python -m fantasy_football.cli load-coaching --file coaching.csv`

To have the **Draft Board** GitHub Action use real coaching, commit a filled
`coaching.csv` to the repo (it is gitignored by default — force-add it:
`git add -f coaching.csv`). The workflow will prefer it over the HC-only template.

## Later

- Import Yahoo base prices to seed/blend the head-to-head ratings.
- Play-by-play enrichment (return TDs by type, passer rating).
- Alembic migrations once the schema stabilizes.
