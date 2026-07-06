# Commit Worker — serverless proxy for "Commit to GitHub"

The Tier Builder is a static site, so it can't safely hold a GitHub write token.
This tiny [Cloudflare Worker](https://workers.cloudflare.com/) can: the app
`POST`s the user's rankings to it, and the Worker commits them to
`picks/u-<id>.csv` using a token stored as a **Worker secret**. The browser
never sees the token.

## What it does

- `POST {id, csv}` → writes/overwrites `picks/u-<id>.csv` on `GH_BRANCH`.
- One file per user (the app's stable random id), so re-submitting just updates
  that user's file — **one vote each** when you Rebuild Master Tiers.
- Validates the payload (id format + `key,rating` rows) and only ever writes
  under `picks/`, so worst-case abuse is junk files there, not arbitrary writes.

## Deploy (Cloudflare free tier)

1. **Create a GitHub token.** Settings → Developer settings → *Fine-grained
   tokens*. Scope it to **only this repo**, permission **Contents: Read and
   write**. Copy the token.

2. **Install + log in to wrangler:**
   ```bash
   npm i -g wrangler
   wrangler login
   ```

3. **Set config.** Edit `wrangler.toml` so `GH_REPO`, `GH_BRANCH`, and
   `ALLOWED_ORIGIN` match your setup (`ALLOWED_ORIGIN` is your GitHub Pages
   origin, e.g. `https://joshua-hsu.github.io`). Then store the token as a
   secret (from this folder):
   ```bash
   wrangler secret put GH_TOKEN   # paste the PAT when prompted
   ```

4. **Deploy:**
   ```bash
   wrangler deploy
   ```
   Copy the deployed URL, e.g. `https://ff-commit-worker.<you>.workers.dev`.

5. **Point the app at it.** Either set the constant in `docs/app.js`
   (`var WORKER_URL = "https://…"`), or add this line to `docs/index.html`
   *before* the `app.js` script tag (no rebuild needed):
   ```html
   <script>window.FF_CONFIG = { workerUrl: "https://ff-commit-worker.<you>.workers.dev" };</script>
   ```

## Test it

```bash
curl -X POST https://ff-commit-worker.<you>.workers.dev \
  -H 'Content-Type: application/json' \
  -d '{"id":"testuser1","csv":"key,rating\np00-0034796,310.1\ndDEN,138.8\n"}'
# -> {"ok":true,"path":"picks/u-testuser1.csv","commit":"…"}
```

Then delete the test file from `picks/` if you don't want it counted.

## Public mode + abuse protection

The site is open to everyone by default (crowdsourced picks). Defenses, in
order:

1. **Origin gate** — cross-origin browser requests not from `ALLOWED_ORIGIN`
   are rejected outright.
2. **Per-IP rate limit** — 20 commits/minute via the native Workers rate-limit
   binding in `wrangler.toml` (generous for humans, hostile to bots).
3. **Turnstile bot check (optional, recommended once traffic grows)** —
   create a Turnstile widget in the Cloudflare dashboard, then:
   `npx wrangler secret put TURNSTILE_SECRET` and set
   `window.FF_CONFIG.turnstileSiteKey` in `docs/index.html`. Headless bots
   fail the check; humans rarely see anything.
4. **Payload caps** — 64 KB body, 400 rows, `key,number[,number]` rows only.
5. **Robust aggregation (downstream)** — the rebuild takes the *median* rating
   across submissions and clamps per-submission comparison counts, so junk
   dilutes rather than dominates.

### Commissioner overwrite (admin endpoint)

The site's `#/admin` page lets the commissioner drag players into tiers and
**Overwrite master tiers**. The write is gated by an `ADMIN_CODE` secret:

```bash
npx wrangler secret put ADMIN_CODE    # unset = endpoint disabled
```

The Worker writes `admin_tiers.csv` and auto-triggers the Rebuild Master
Tiers workflow (needs the PAT to also have **Actions: Read and write**; if it
doesn't, the file is still saved and the rebuild reports "run manually").
Pins apply after the crowd blend at full weight and are consumed by one
rebuild.

### Private-league mode (optional)

Set `npx wrangler secret put LEAGUE_CODE` to gate writes behind a shared
passcode. Nobody is prompted unless the server demands it: on a 401 the app
asks once, remembers the answer, and re-prompts if it's ever rejected. Delete
the secret to go public again; rotate it if it leaks.

## Security notes

- The GitHub token lives only in Cloudflare; it's never shipped to browsers.
- Cross-origin requests are **rejected** (not just CORS-headered) unless they
  come from `ALLOWED_ORIGIN`; no-Origin callers (curl) still need the code.
- Payloads are capped (64 KB, 400 rows) and every row must parse as
  `key,number[,number]`.
- The Worker hard-codes the `picks/u-<id>.csv` path, so callers can't write
  anywhere else in the repo. Worst case with a leaked code is junk CSVs in
  `picks/` — delete them and rotate the secret.
