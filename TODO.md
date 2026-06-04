# TODO

## Coaching data (OC)

Head coaches load automatically from nflverse, but the **offensive coordinator**
is not in nflverse and shows **TBD** on the cards and the draft board's Coaching
sheet until filled.

To fill it:

1. `python -m fantasy_football.cli coaching-template --out coaching.csv`
   (writes all 32 teams with head coaches pre-filled)
2. Fill the `offensive_coordinator` column.
3. `python -m fantasy_football.cli load-coaching --file coaching.csv`

To have the **Draft Board** GitHub Action use real coaching, commit a filled
`coaching.csv` to the repo (it is gitignored by default — force-add it:
`git add -f coaching.csv`). The workflow will prefer it over the HC-only template.

## Later

- Import Yahoo base prices to seed/blend the head-to-head ratings.
- Play-by-play enrichment (return TDs by type, passer rating).
- Alembic migrations once the schema stabilizes.
