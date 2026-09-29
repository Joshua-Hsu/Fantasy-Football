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
- **API path check pass (2026-09-29)** - `cli scout-run` makes a second
  call per note handing Gemini its own note plus the verified box-score
  lines and asking it to correct any claim the numbers contradict (the
  same check a paste gets by hand). Auto notes are labeled "box-checked";
  `--no-check` restores the single call ("unverified"). Not yet measured
  against the hand check - compare on the first week the key is live.
- **First live runs (2026-09-29)**: run 1 404'd on the retired hard-coded
  `gemini-2.5-flash` (fixed: model resolved from the API); run 2/3 got HTTP
  429 "check your plan and billing" on `gemini-3.8-flash` even with pacing
  and retries, and the fallback `gemini-2.5-flash` is "no longer available
  to new users". Cause: in 2026 Google Search grounding on 3.x models needs
  a billing-enabled project (Tier 1: 5,000 grounded prompts/month free,
  then $14/1k; tokens ~$0.75/M in, $3.75/M out on 3.8 Flash - about a
  quarter per week for 32 calls). The free tier keeps grounding only on the
  2.5 models, which new keys cannot use. `scout-run` now probes the key
  first and stops with that diagnosis instead of burning the paced retries.
- **Run 4 (2026-09-29, billing on, gemini-3.8-flash, box-checked)**: 30 of
  32 notes in 18 minutes (CHI/MIN lost to a transient 503 - now retried).
  Quality vs the hand-verified week-2 set: detail is richer (package %,
  blitz %, who covered whom), every named stat line checked out against
  the DB, and the check pass DID correct the McCaffrey claim ("held to 2.3
  ypc, but failed to bottle up the run game as McCaffrey scored 2 TDs").
  Failure: sections ran past 900 chars and the blind cap dropped the
  'Fantasy read' and next-opponent lines on most teams - the parts that
  matter for lineups. Fix: the check pass now sees the FULL section and is
  told to compress to <850 chars ending with the 'Fantasy read:' sentence
  and a 'Wk<N> <opp>:' line. The 30 wk2 auto rows were removed after
  grading (the verified hand notes stay); week 3 is the first real run.
- **Run 5 (2026-09-29, week 3 - first real weekly run)**: 32/32 notes in
  21 minutes, all ending with 'Fantasy read:' and a 'Wk4 <opp>:' line;
  DEN and IND spot-checked line by line against the DB with zero
  discrepancies. Remaining nit: the label pushed a few past 900 chars -
  the body is now capped to what the label leaves. Automated path is the
  weekly default from here; pastes stay welcome as overrides (newest wins).
- **Cost (2026-09-29)**: the AI Studio project showed $1.45 after the
  day's runs - about 97 successful calls, ~1.5c each. Cause: every weekly
  run made 48 calls, not 16 - the 32 check-pass calls were grounded and on
  the thinking model with no budget, and the probe added a grounded call.
  Now: 16 grounded scouting calls with a 1024-token thinking budget, 32
  ungrounded check-pass calls on flash-lite with thinking off, a plain
  probe, and every run prints tokens + an estimated cost (grounding priced
  as if metered, so the real bill is at or below the line). Expected
  ~$0.10-0.20 per run; the first cheap run's AI Studio delta goes here.
- **Run 7 (cheap path, week 3 forced)**: 4 minutes, cost line `flash in
  10k / out+think 19k; grounded 7 of 16; est. $0.18` - but every lite
  check-pass call got HTTP 400 INVALID_ARGUMENT (flash-lite rejects the
  thinking budget), so the raw truncated sections were filed without the
  fantasy read, and only 7 of 16 scouting answers were actually grounded
  (the model skipped search on the rest; two games came back with no team
  sections at all). Fixes: `gemini_call` drops a rejected generation option
  and retries; an ungrounded scouting answer is retried once with a search
  nudge; empty parses are logged. The 28 degraded rows were removed.
- **Run 8 (fixes for lite 400 + ungrounded)**: 8 minutes; every scouting
  answer grounded (7 needed the nudge); lite check pass working; cost line
  `flash in 14k / out+think 26k; lite in 23k / out 6k; grounded 16 of 46;
  est. $0.34` (about $0.12 of that is tokens, the rest is grounding priced
  as if metered - the real bill is between). Two leftovers: 4 games parsed
  to nothing because Gemini used long markdown headings ("### Miami
  Dolphins Defense: Schematic Breakdown & Fantasy Outlook") that the
  60-char heading rule rejected, and the lite rewrite dropped the 'Fantasy
  read' on 5 of 24 notes. Fixes: headings detected on the raw markdown
  line at any length; `ensure_structure` re-attaches the fantasy read from
  the raw section when the rewrite loses it.
- Tweak ideas not yet tried: ask for "the three plays that decided it" to
  surface scheme detail; ask for "who covered the slot" by name; ask
  whether the box count changed by down and distance.
