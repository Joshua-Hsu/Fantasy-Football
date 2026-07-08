// Cloudflare Worker: serverless proxy behind the Tier Builder's "Commit to
// GitHub" button. The app is a static site and can't hold a write token, so it
// POSTs {id, csv, code?, ts?} here and this Worker commits picks/u-<id>.csv
// using a token stored as a Worker secret. The token never reaches the browser.
//
// Trust model: PUBLIC crowdsourcing. Anyone may submit picks; abuse is
// contained by (in order): hard Origin rejection, per-IP rate limiting,
// optional Turnstile bot check, optional league passcode, and payload caps.
// The blend math downstream is robust too (median across users, comps
// clamps), so junk submissions dilute instead of dominate. Worst case is
// junk CSVs under picks/ — the Worker cannot write anywhere else.
//
// Config (wrangler vars + secrets):
//   GH_TOKEN         (secret) fine-grained PAT, Contents: Read and write on the
//                    repo (add Actions: Read and write to auto-trigger rebuilds)
//   ADMIN_CODE       (secret) commissioner passcode for the tier-overwrite
//                    endpoint; unset = admin endpoint disabled
//   TURNSTILE_SECRET (secret, optional) enables Cloudflare Turnstile bot checks;
//                    pair with FF_CONFIG.turnstileSiteKey on the site
//   LEAGUE_CODE      (secret, optional) private-league mode: submissions must
//                    carry the passcode; leave unset for open/public mode
//   GH_REPO          "owner/repo", e.g. "Joshua-Hsu/Fantasy-Football"
//   GH_BRANCH        branch to commit to (default "main")
//   ALLOWED_ORIGIN   your site origin, e.g. "https://joshua-hsu.github.io" ("*" = any)
//   RATE_LIMITER     (binding, wrangler.toml) per-IP commit rate limit

const API = "https://api.github.com";
const MAX_BODY = 64 * 1024;   // bytes
const MAX_ROWS = 400;         // pick rows per submission (the pool is ~250)

function corsHeaders(origin, allowed) {
  const allow = allowed === "*" ? "*" : (origin && origin === allowed ? origin : allowed);
  return {
    "Access-Control-Allow-Origin": allow || "*",
    "Access-Control-Allow-Methods": "POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
    "Vary": "Origin",
  };
}

function json(body, status, headers) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", ...headers },
  });
}

// Constant-time-ish string compare so the passcode can't be guessed
// character by character from response timing.
function safeEqual(a, b) {
  const enc = new TextEncoder();
  const ab = enc.encode(String(a));
  const bb = enc.encode(String(b));
  if (ab.length !== bb.length) return false;
  let diff = 0;
  for (let i = 0; i < ab.length; i++) diff |= ab[i] ^ bb[i];
  return diff === 0;
}

export default {
  async fetch(request, env) {
    const origin = request.headers.get("Origin") || "";
    const allowed = env.ALLOWED_ORIGIN || "*";
    const ch = corsHeaders(origin, allowed);

    if (request.method === "OPTIONS") return new Response(null, { status: 204, headers: ch });
    if (request.method !== "POST") return json({ error: "POST only" }, 405, ch);

    // Hard origin gate: browsers always send Origin on cross-site POSTs, so a
    // mismatch means the request isn't coming from our site.
    if (allowed !== "*" && origin && origin !== allowed) {
      return json({ error: "origin not allowed" }, 403, ch);
    }

    // Per-IP rate limit (native Workers binding; silently skipped if absent).
    const ip = request.headers.get("CF-Connecting-IP") || "unknown";
    if (env.RATE_LIMITER) {
      try {
        const { success } = await env.RATE_LIMITER.limit({ key: ip });
        if (!success) return json({ error: "slow down - try again in a minute" }, 429, ch);
      } catch (e) { /* binding hiccup: fail open */ }
    }

    const raw = await request.text();
    if (raw.length > MAX_BODY) return json({ error: "payload too large" }, 413, ch);
    let data;
    try { data = JSON.parse(raw); } catch { return json({ error: "bad JSON" }, 400, ch); }

    // Optional bot check: verify the Turnstile token when configured.
    if (env.TURNSTILE_SECRET) {
      const form = new FormData();
      form.append("secret", env.TURNSTILE_SECRET);
      form.append("response", String(data.ts || ""));
      form.append("remoteip", ip);
      const vr = await fetch("https://challenges.cloudflare.com/turnstile/v0/siteverify",
                             { method: "POST", body: form });
      const verdict = await vr.json().catch(() => ({}));
      if (!verdict.success) return json({ error: "bot check failed - reload and retry" }, 403, ch);
    }

    // Commissioner endpoint: overwrite master tiers. Disabled unless the
    // ADMIN_CODE secret exists; wrong code -> 401. Writes admin_tiers.csv and
    // auto-triggers the Rebuild Master Tiers workflow (best effort).
    if (data.kind === "admin") {
      if (!env.ADMIN_CODE) return json({ error: "admin disabled" }, 403, ch);
      if (!safeEqual(data.code || "", env.ADMIN_CODE)) {
        return json({ error: "bad admin code" }, 401, ch);
      }
      const acsv = String(data.csv || "");
      const arows = acsv.split(/\r?\n/).map((s) => s.trim())
        .filter((r) => r && !/^key\s*,/i.test(r));
      if (!arows.length) return json({ error: "no rows" }, 400, ch);
      if (arows.length > MAX_ROWS) return json({ error: "too many rows" }, 413, ch);
      for (const r of arows) {
        // key,rating[,tier] — or "key,," which releases a pinned player back
        // to the crowd (both fields empty).
        const p = r.split(",");
        if (p.length < 2) return json({ error: `bad row: ${r}` }, 400, ch);
        const rating = (p[1] || "").trim();
        const tier = (p[2] || "").trim();
        const release = rating === "" && tier === "";
        if (!release && isNaN(parseFloat(rating))) return json({ error: `bad row: ${r}` }, 400, ch);
        if (tier !== "" && !/^\d{1,2}$/.test(tier)) return json({ error: `bad row: ${r}` }, 400, ch);
      }
      const arepo = env.GH_REPO;
      const abranch = env.GH_BRANCH || "main";
      const apath = "admin_tiers.csv";
      const aurl = `${API}/repos/${arepo}/contents/${apath}`;
      const agh = {
        "Authorization": `Bearer ${env.GH_TOKEN}`,
        "Accept": "application/vnd.github+json",
        "User-Agent": "ff-commit-worker",
        "Content-Type": "application/json",
      };
      let asha;
      const ahead = await fetch(`${aurl}?ref=${encodeURIComponent(abranch)}`, { headers: agh });
      if (ahead.status === 200) asha = (await ahead.json()).sha;
      else if (ahead.status !== 404) return json({ error: `github read ${ahead.status}` }, 502, ch);
      const aput = await fetch(aurl, {
        method: "PUT",
        headers: agh,
        body: JSON.stringify({
          message: "commissioner: overwrite master tiers",
          content: btoa(unescape(encodeURIComponent(acsv))),
          branch: abranch,
          ...(asha ? { sha: asha } : {}),
        }),
      });
      if (!aput.ok) {
        const detail = (await aput.text()).slice(0, 300);
        return json({ error: `github write ${aput.status}`, detail }, 502, ch);
      }
      // Kick the rebuild so the overwrite goes live without a manual step.
      const disp = await fetch(
        `${API}/repos/${arepo}/actions/workflows/master-tiers.yml/dispatches`,
        { method: "POST", headers: agh, body: JSON.stringify({ ref: abranch }) });
      return json({
        ok: true, path: apath, rows: arows.length,
        rebuild: disp.status === 204
          ? "triggered"
          : `not triggered (${disp.status}) - run Rebuild Master Tiers manually`,
      }, 200, ch);
    }

    // Optional private-league gate.
    if (env.LEAGUE_CODE && !safeEqual(data.code || "", env.LEAGUE_CODE)) {
      return json({ error: "bad league code" }, 401, ch);
    }

    const id = String(data.id || "").trim();
    const csv = String(data.csv || "");
    if (!/^[a-z0-9]{4,40}$/i.test(id)) return json({ error: "bad id" }, 400, ch);
    if (!csv.trim()) return json({ error: "empty csv" }, 400, ch);

    // Sanity: every non-empty, non-header row must be key,number[,number].
    const rows = csv.split(/\r?\n/).map((s) => s.trim())
      .filter((r) => r && !/^key\s*,/i.test(r));
    if (!rows.length) return json({ error: "no pick rows" }, 400, ch);
    if (rows.length > MAX_ROWS) return json({ error: "too many rows" }, 413, ch);
    for (const r of rows) {
      const p = r.split(",");
      if (p.length < 2 || isNaN(parseFloat(p[1]))) return json({ error: `bad row: ${r}` }, 400, ch);
    }

    const repo = env.GH_REPO;
    const branch = env.GH_BRANCH || "main";
    const path = `picks/u-${id}.csv`;
    const url = `${API}/repos/${repo}/contents/${path}`;
    const gh = {
      "Authorization": `Bearer ${env.GH_TOKEN}`,
      "Accept": "application/vnd.github+json",
      "User-Agent": "ff-commit-worker",
    };

    // Look up the existing SHA so we overwrite (one file per user).
    let sha;
    const head = await fetch(`${url}?ref=${encodeURIComponent(branch)}`, { headers: gh });
    if (head.status === 200) sha = (await head.json()).sha;
    else if (head.status !== 404) return json({ error: `github read ${head.status}` }, 502, ch);

    // UTF-8 safe base64.
    const content = btoa(unescape(encodeURIComponent(csv)));
    const put = await fetch(url, {
      method: "PUT",
      headers: { ...gh, "Content-Type": "application/json" },
      body: JSON.stringify({
        message: `rankings: ${path}`,
        content,
        branch,
        ...(sha ? { sha } : {}),
      }),
    });
    if (!put.ok) {
      const detail = (await put.text()).slice(0, 300);
      return json({ error: `github write ${put.status}`, detail }, 502, ch);
    }
    const out = await put.json();
    return json({ ok: true, path, commit: out.commit && out.commit.sha }, 200, ch);
  },
};
