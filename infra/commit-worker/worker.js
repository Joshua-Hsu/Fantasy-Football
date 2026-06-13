// Cloudflare Worker: serverless proxy behind the Tier Builder's "Commit to
// GitHub" button. The app is a static site and can't hold a write token, so it
// POSTs {id, csv} here and this Worker commits picks/u-<id>.csv using a token
// stored as a Worker secret. The token never reaches the browser.
//
// Config (wrangler vars + secret):
//   GH_TOKEN       (secret) fine-grained PAT, Contents: Read and write on the repo
//   GH_REPO        "owner/repo", e.g. "Joshua-Hsu/Fantasy-Football"
//   GH_BRANCH      branch to commit to (default "main")
//   ALLOWED_ORIGIN your site origin, e.g. "https://joshua-hsu.github.io" ("*" = any)

const API = "https://api.github.com";

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

export default {
  async fetch(request, env) {
    const origin = request.headers.get("Origin") || "";
    const ch = corsHeaders(origin, env.ALLOWED_ORIGIN || "*");

    if (request.method === "OPTIONS") return new Response(null, { status: 204, headers: ch });
    if (request.method !== "POST") return json({ error: "POST only" }, 405, ch);

    let data;
    try { data = await request.json(); } catch { return json({ error: "bad JSON" }, 400, ch); }

    const id = String(data.id || "").trim();
    const csv = String(data.csv || "");
    if (!/^[a-z0-9]{4,40}$/i.test(id)) return json({ error: "bad id" }, 400, ch);
    if (!csv.trim()) return json({ error: "empty csv" }, 400, ch);

    // Sanity: every non-empty, non-header row must be key,number.
    const rows = csv.split(/\r?\n/).map((s) => s.trim())
      .filter((r) => r && !/^key\s*,/i.test(r));
    if (!rows.length) return json({ error: "no pick rows" }, 400, ch);
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
