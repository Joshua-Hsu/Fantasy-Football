// Cloudflare Worker: serverless proxy behind the Tier Builder's "Commit to
// GitHub" button. The app is a static site and can't hold a write token, so it
// POSTs {id, csv, code} here and this Worker commits picks/u-<id>.csv using a
// token stored as a Worker secret. The token never reaches the browser.
//
// Abuse surface: the site is public, so anyone can find this endpoint. Three
// gates keep the repo safe: a shared LEAGUE_CODE (only league members can
// write), hard Origin rejection (not just CORS response headers), and payload
// caps. Worst case with a leaked code is junk CSVs under picks/ — rotate the
// secret to lock it out again.
//
// Config (wrangler vars + secrets):
//   GH_TOKEN       (secret) fine-grained PAT, Contents: Read and write on the repo
//   LEAGUE_CODE    (secret) shared passcode league members enter once in the app;
//                  leave unset for open bootstrap mode
//   GH_REPO        "owner/repo", e.g. "Joshua-Hsu/Fantasy-Football"
//   GH_BRANCH      branch to commit to (default "main")
//   ALLOWED_ORIGIN your site origin, e.g. "https://joshua-hsu.github.io" ("*" = any)

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
    // mismatch means the request isn't coming from our site. (Requests with no
    // Origin at all — curl and scripts — still have to know the league code.)
    if (allowed !== "*" && origin && origin !== allowed) {
      return json({ error: "origin not allowed" }, 403, ch);
    }

    const raw = await request.text();
    if (raw.length > MAX_BODY) return json({ error: "payload too large" }, 413, ch);
    let data;
    try { data = JSON.parse(raw); } catch { return json({ error: "bad JSON" }, 400, ch); }

    // League gate: submissions must carry the shared passcode.
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
