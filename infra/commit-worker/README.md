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

## League code (abuse protection)

The site is public, so the Worker gates writes behind a shared passcode:

```bash
npx wrangler secret put LEAGUE_CODE    # or dashboard: Settings -> Variables -> Secret
```

Pick any phrase, share it with your league; the app asks each member for it
once (stored in their browser) and sends it with every commit. Wrong/missing
code -> 401, and the app re-prompts. If the code ever leaks, set a new secret
value and tell the league — old submissions are unaffected. While the secret
is unset the Worker is open (bootstrap mode).

## Security notes

- The GitHub token lives only in Cloudflare; it's never shipped to browsers.
- Cross-origin requests are **rejected** (not just CORS-headered) unless they
  come from `ALLOWED_ORIGIN`; no-Origin callers (curl) still need the code.
- Payloads are capped (64 KB, 400 rows) and every row must parse as
  `key,number[,number]`.
- The Worker hard-codes the `picks/u-<id>.csv` path, so callers can't write
  anywhere else in the repo. Worst case with a leaked code is junk CSVs in
  `picks/` — delete them and rotate the secret.
