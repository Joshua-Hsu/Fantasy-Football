# Prompts

Plain-text prompt templates the toolkit hands to the user (or to an API).
Edit the file, not the code: `scout.py` reads `defense_scout.txt` at run
time and falls back to a built-in copy only if the file is missing.

## defense_scout.txt

The weekly two-defense scouting question for Gemini (one game per paste).
Placeholders: `{a}` / `{b}` full team names, `{a_short}` / `{b_short}` city
names, `{a_next}` / `{b_next}` next opponents as "the Rams" / "at the
Rams", `{week}`, `{prev}`, `{year}`. Printed by
`python -m fantasy_football.cli scout-prompts --game DEN-JAX`.

### Changelog (what worked, what did not)

- **v1 (2026-09-22, retired)** - a structured "Scout BOTH defenses ... For
  EACH defense give: 1. scheme ... 5. fantasy translation ... under 900
  characters. Cite sources." Gemini returned one thin paragraph on one team
  and a link. Reads like a model request; it under-delivers.
- **v2 (2026-09-23, current)** - the fan question above. Returned two
  full sections per paste (scheme, package rates, pressure names, injuries
  with fill-ins, what the offense did, a fantasy translation for the next
  opponent) on 15 of 16 games in week 2. Kept exactly as pasted; the user
  asked that no extra stats be appended to the copy-paste version.
- Known failure modes to check every time (the verifier catches them):
  it narrates a "shutdown" the box score contradicts (Aaron Jones 23-105,
  McCaffrey 2 TD, Schultz 12-140), and it credits a defense for an injury
  (Saquon's stinger). It is reliable on snap shares, package rates and
  named pressure players.
- Tweak ideas not yet tried: ask for "the three plays that decided it" to
  surface scheme detail; ask for "who covered the slot" by name; ask
  whether the box count changed by down and distance.
