/* FF Tier Builder - static head-to-head pick game.
   Seeds an in-browser Elo from the computed auction value, refines it with your
   A/B picks, derives tiers, and exports a tiers CSV you can feed back into the
   valuation CLI. All state lives in localStorage - no server. */
(function () {
  "use strict";

  var DATA = (window.FF_DATA || { positions: {} }).positions;
  var ORDER = ["QB", "RB", "WR", "TE", "K", "DST"];
  var TIER_K = { QB: 6, RB: 8, WR: 8, TE: 6, K: 5, DST: 6 };
  // SCALE governs how decisive a pick is; K is the per-pick rating step. Players
  // are seeded on a continuous value scale (adjacent players ~10-30 apart), so a
  // pick (~K/2 points) moves you past your neighbours - the ranking is driven by
  // the head-to-head relations, not the seed.
  var SCALE = 400, K = 24, NEAR = 4, NUDGE = 10;
  var STORE = "ff_tier_state_v3";  // v3: seed from master continuous ratings
  // Commit-to-GitHub goes through a tiny serverless proxy (Cloudflare Worker)
  // that holds the write token; the browser never sees it. Set this to your
  // deployed Worker URL (see infra/commit-worker/README.md). Empty = not wired.
  var WORKER_URL = (window.FF_CONFIG && window.FF_CONFIG.workerUrl) || "";
  // Stable anonymous per-browser id so each person's rankings accumulate into a
  // single file (picks/u-<id>.csv) that re-submits overwrite - one vote each.
  var UID_STORE = "ff_user_id";
  function userId() {
    var id = localStorage.getItem(UID_STORE);
    if (!id) {
      id = Date.now().toString(36) + Math.random().toString(36).slice(2, 8);
      localStorage.setItem(UID_STORE, id);
    }
    return id;
  }
  // Flatten + index.
  var ALL = [];
  ORDER.forEach(function (p) { (DATA[p] || []).forEach(function (e) { ALL.push(e); }); });
  var BYKEY = {}; ALL.forEach(function (e) { BYKEY[e.key] = e; });

  // ---- state ----
  function load() {
    var s; try { s = JSON.parse(localStorage.getItem(STORE)); } catch (e) {}
    s = s || {};
    s.ratings = s.ratings || {};
    s.comps = s.comps || {};
    s.picks = s.picks || 0;
    s.notes = s.notes || {};   // {pos: {tier: "hand-written tier description"}}
    var healed = false;
    ALL.forEach(function (e) {              // seed any missing
      if (s.ratings[e.key] == null) s.ratings[e.key] = e.seed;
      if (s.comps[e.key] == null) s.comps[e.key] = 0;
      // Heal ratings cached from the poisoned-ladder era: a stored value near
      // zero for a player the data now seeds on the real scale was never a
      // genuine pick result - re-baseline him from the fresh seed.
      if (s.ratings[e.key] <= 0.5 && e.seed > 0.5) {
        s.ratings[e.key] = e.seed;
        healed = true;
      }
    });
    if (healed) localStorage.setItem(STORE, JSON.stringify(s));
    return s;
  }
  function save(s) { localStorage.setItem(STORE, JSON.stringify(s)); }
  var S = load();

  // ---- elo ----
  function expected(a, b) { return 1 / (1 + Math.pow(10, (b - a) / SCALE)); }
  function pick(winner, loser) {
    var rw = S.ratings[winner], rl = S.ratings[loser];
    var e = expected(rw, rl);
    S.ratings[winner] = rw + K * (1 - e);
    S.ratings[loser] = rl + K * (0 - (1 - e));
    S.comps[winner]++; S.comps[loser]++; S.picks++;
    save(S);
  }

  var lastPair = [];
  function matchup(pos, avoid) {
    avoid = avoid || [];
    var pool = (DATA[pos] || []).slice();
    if (pool.length < 2) return null;
    var elig = pool.filter(function (e) { return avoid.indexOf(e.key) < 0; });
    if (elig.length < 2) elig = pool;  // tiny pool: can't avoid
    var fewest = Math.min.apply(null, elig.map(function (e) { return S.comps[e.key]; }));
    var cands = elig.filter(function (e) { return S.comps[e.key] <= fewest + 1; });
    var a = cands[Math.floor(Math.random() * cands.length)];
    var others = elig.filter(function (e) { return e.key !== a.key; })
      .sort(function (x, y) {
        return Math.abs(S.ratings[x.key] - S.ratings[a.key]) -
               Math.abs(S.ratings[y.key] - S.ratings[a.key]);
      });
    var b = others[Math.floor(Math.random() * Math.min(NEAR, others.length))];
    return [a, b];
  }

  // ---- ranks / tiers ----
  function posRank(e) {
    var r = S.ratings[e.key], n = 1;
    (DATA[e.pos] || []).forEach(function (o) { if (S.ratings[o.key] > r) n++; });
    return n;
  }
  function overallRank(e) {
    var r = S.ratings[e.key], n = 1;
    ALL.forEach(function (o) { if (S.ratings[o.key] > r) n++; });
    return n;
  }
  function kmeans1d(vals, k) {
    var n = vals.length; if (!n) return [];
    var distinct = vals.slice().sort(function (a, b) { return a - b; })
      .filter(function (v, i, a) { return i === 0 || v !== a[i - 1]; });
    k = Math.max(1, Math.min(k, distinct.length));
    if (k === 1) return vals.map(function () { return 1; });
    var ord = vals.slice().sort(function (a, b) { return a - b; });
    var c = []; for (var i = 0; i < k; i++) c.push(ord[Math.round(i * (n - 1) / (k - 1))]);
    for (var it = 0; it < 100; it++) {
      var sums = new Array(k).fill(0), cnt = new Array(k).fill(0);
      vals.forEach(function (v) { var j = nearest(v, c); sums[j] += v; cnt[j]++; });
      var moved = false;
      for (var j = 0; j < k; j++) { if (cnt[j]) { var m = sums[j] / cnt[j]; if (m !== c[j]) { c[j] = m; moved = true; } } }
      if (!moved) break;
    }
    var rank = c.map(function (_, i) { return i; }).sort(function (a, b) { return c[b] - c[a]; });
    var tierOf = {}; rank.forEach(function (ci, t) { tierOf[ci] = t + 1; });
    return vals.map(function (v) { return tierOf[nearest(v, c)]; });
  }
  function nearest(v, c) { var bj = 0, bd = Infinity; for (var j = 0; j < c.length; j++) { var d = Math.abs(v - c[j]); if (d < bd) { bd = d; bj = j; } } return bj; }
  function sizedTiers(values, k, maxSize) {
    // Gap-aware tiers (mirrors valuation.assign_sized_tiers): start a new tier at
    // a notable drop in rating so elite guys break out into their own small
    // tiers, capped at maxSize. Top k-2 tiers form this way; the rest split
    // across the last two (uncapped) tiers. `values` is best->worst.
    maxSize = maxSize || 7;
    var n = values.length, labels = new Array(n);
    if (n === 0) return labels;
    var capped = Math.max(k - 2, 1);
    var gaps = []; for (var g = 0; g < n - 1; g++) gaps.push(values[g] - values[g + 1]);
    var positive = gaps.filter(function (x) { return x > 0; }).sort(function (a, b) { return a - b; });
    var thr = positive.length ? positive[Math.floor(0.75 * (positive.length - 1))] : Infinity;
    // Floor: a break needs >= 5% of the position's spread, so a 2-3% dip
    // between near-equals never splits a tier (mirrors assign_sized_tiers).
    if (n > 1) thr = Math.max(thr, 0.05 * (values[0] - values[n - 1]));
    var tier = 1, count = 0, i = 0;
    while (i < n) {
      labels[i] = tier; count++;
      var lastCapped = tier >= capped;
      var gapBreak = i < n - 1 && gaps[i] >= thr;
      i++;
      if (lastCapped) {
        if (count >= maxSize) break;
      } else if (count >= maxSize || gapBreak) {
        tier++; count = 0;
      }
    }
    var rest = n - i;
    if (rest > 0) {
      var half = Math.ceil(rest / 2);
      for (var m = i; m < n; m++) labels[m] = tier + 1 + ((m - i) < half ? 0 : 1);
    }
    return labels;
  }
  function tiersFor(pos) {
    var pool = (DATA[pos] || []).slice().sort(function (a, b) { return S.ratings[b.key] - S.ratings[a.key]; });
    var labels = sizedTiers(pool.map(function (e) { return S.ratings[e.key]; }), TIER_K[pos] || 6, 7);
    var out = {}; pool.forEach(function (e, i) { out[e.key] = labels[i]; }); return out;
  }

  // ---- views ----
  var app = document.getElementById("app");
  function esc(s) { return String(s == null ? "" : s).replace(/[&<>"]/g, function (c) { return ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]; }); }
  function nav(extra) {
    return "<div class='nav'><a class='brand' href='#/'><span class='ball'>&#127944;</span> Tier Builder</a>" + (extra || "") +
      "<span class='spacer'></span><span class='pill'>" + S.picks + " picks</span></div>";
  }

  function home() {
    var g = ORDER.filter(function (p) { return (DATA[p] || []).length; }).map(function (p) {
      var picks = (DATA[p] || []).reduce(function (a, e) { return a + S.comps[e.key]; }, 0);
      return "<button onclick=\"location.hash='#/play/" + p + "'\">" +
        "<span class='pos'>" + p + "</span>" +
        "<span class='sub'>" + Math.round(picks / 2) + " picks &middot; play &raquo;</span></button>";
    }).join("");
    app.innerHTML = nav() +
      "<h1>Tier Builder</h1><p class='lead'>Pick who you'd rather draft. Your picks " +
      "become user ratings &rarr; tiers. Export to edit by hand, import to load it back.</p>" +
      "<div class='pos-grid'>" + g + "</div>" +
      "<div class='actions'>" +
      "<button class='btn btn-primary' onclick='FF.commitPicks()'>&#128640; Commit to GitHub</button>" +
      "<button class='btn' onclick=\"location.hash='#/packet'\">&#128424; My draft packet</button>" +
      "<button class='btn' onclick='FF.exportTiers()'>&#11015; Export tiers CSV</button>" +
      "<label class='btn' style='cursor:pointer'>&#11014; Import tiers CSV" +
      "<input type='file' accept='.csv' style='display:none' onchange='FF.importTiers(this)'></label>" +
      "</div><p class='muted'>Commit sends your rankings to the shared " +
      "<code>picks/</code> database; run the Rebuild Master action to fold " +
      "everyone in. Export is just your personal copy.</p>";
  }

  function coachName(name, isNew) {
    // Shade coaches new to the role this season - same soft-chip language as
    // the rookie badge, so "highlighted = new" reads consistently.
    if (!name) return "TBD";
    return isNew
      ? "<span class='coach-new' title='New to this role this season'>" + esc(name) + "</span>"
      : esc(name);
  }

  function statSegs(e) {
    // Position-aware last-year box line, built from the structured cols so we
    // can style value/label pairs instead of dumping a raw text string.
    var c = e.cols || {};
    var segs = [];
    var add = function (val, label) {
      if (val !== "" && val != null) segs.push([val, label]);
    };
    if (e.pos === "QB") {
      add(c.PaYds, "pa yd"); add(c.PaTD, "pa td"); add(c.INT, "int");
      add(c.RuYds, "ru yd"); add(c.RuTD, "ru td");
    } else if (e.pos === "RB") {
      add(c.RuAtt, "car"); add(c.RuYds, "ru yd"); add(c.RuTD, "ru td");
      add(c.Rec, "rec"); add(c.ReYds, "re yd"); add(c.ReTD, "re td");
    } else if (e.pos === "WR" || e.pos === "TE") {
      add(c.Tgt, "tgt"); add(c.Rec, "rec"); add(c.ReYds, "yd"); add(c.ReTD, "td");
      add(c["Tgt%"], "tgt%");
    } else if (e.pos === "K") {
      if (c.FGM !== "" && c.FGM != null && c.FGA !== "" && c.FGA != null) {
        segs.push([c.FGM + "/" + c.FGA, "fg"]);
      }
      add(c.XPM, "xp");
    } else if (e.pos === "DST") {
      add(c.DefPA, "pa/g"); add(c.DefSk, "sack"); add(c.DefINT, "int"); add(c.DefTD, "td");
    }
    return segs;
  }

  function statLine(e) {
    if (e.rookie) return "";
    var segs = statSegs(e);
    if (!segs.length) {   // old data.js without cols: fall back to the raw text
      return e.stat ? "<div class='muted'>" + esc(e.stat) + "</div>" : "";
    }
    return "<div class='statline'>" + segs.map(function (s) {
      return "<span class='seg'><b>" + esc(s[0]) + "</b><i>" + esc(s[1]) + "</i></span>";
    }).join("") + "</div>";
  }

  function teamRankLine(e) {
    // Team-offense ranks (the TEAM's, not the player's) — shown under the
    // coaches so the player's situation reads as one block.
    if (["QB", "RB", "WR", "TE"].indexOf(e.pos) < 0) return "";
    var c = e.cols || {};
    if (c.TmYdsRk === "" || c.TmYdsRk == null) {
      return e.tmoff ? "<div class='muted'>" + esc(e.tmoff) + "</div>" : "";
    }
    var rk = function (n, label) {
      var cls = n <= 10 ? " good" : (n >= 23 ? " bad" : "");
      return "<span class='seg" + cls + "'><b>#" + n + "</b><i>" + label + "</i></span>";
    };
    return "<div class='tm-rank'><span class='tm-lab'>" + esc(e.team || "") +
      " offense</span>" + rk(c.TmYdsRk, "yds") + rk(c.TmPassRk, "pass") +
      rk(c.TmRushRk, "rush") + "</div>";
  }

  function statBlock(e) {
    if (!e) return "<div class='muted'>no data</div>";
    var rookie = e.rookie ? "<span class='badge'>R</span>" : "";
    var stat = function (v) { return e.rookie ? "&mdash;" : v.toFixed(1); };
    var draftLine = e.rookie
      ? "<div class='muted'>Rookie" + (e.draft ? " &middot; " + esc(e.draft) : "") + "</div>"
      : "";
    return "<div class='name'>" + esc(e.name) + rookie + "</div>" +
      "<div class='muted'>" + esc(e.pos) + " &middot; " + esc(e.team || "TBD") +
      " &middot; Pos #" + posRank(e) + " &middot; Ovr #" + overallRank(e) + "</div>" +
      "<div class='muted'>HC " + coachName(e.hc, e.hcN) + " &middot; OC " + coachName(e.oc, e.ocN) +
      "</div>" +
      teamRankLine(e) + draftLine +
      "<div class='stats'>" +
      "<div class='row'><span>Last-yr total</span><b>" + stat(e.total) + "</b></div>" +
      "<div class='row'><span>Last-yr PPG</span><b>" + stat(e.ppg) + "</b></div>" +
      "<div class='row'><span>3-yr weighted</span><b>" + stat(e.w3yr) + "</b></div></div>" +
      statLine(e);
  }

  function play(pos) {
    var m = matchup(pos, lastPair);
    if (!m) { app.innerHTML = nav() + "<h1>" + pos + "</h1><p>Not enough " + pos + " players.</p>"; return; }
    var a = m[0], b = m[1];
    lastPair = [a.key, b.key];  // next pair will avoid these two
    app.innerHTML = nav(" &middot; <a href='#/rank/" + pos + "'>" + pos + " ranking</a>") +
      "<h1>" + pos + " &mdash; who'd you rather?</h1>" +
      "<div class='cards'>" +
        "<button class='card' onclick=\"FF.choose('" + a.key + "','" + b.key + "','" + pos + "')\">" + statBlock(a) + "</button>" +
        "<button class='card' onclick=\"FF.choose('" + b.key + "','" + a.key + "','" + pos + "')\">" + statBlock(b) + "</button>" +
      "</div><p class='vs'>" +
        "<button class='btn' onclick=\"FF.again('" + pos + "')\">&#8635; different pair</button> " +
        "<button class='btn' onclick=\"FF.noPick('" + a.key + "','" + b.key + "','" + pos + "')\">" +
        "&#8856; no pick</button>" +
      "</p>";
  }

  function rank(pos) {
    var tiers = tiersFor(pos);
    var pool = (DATA[pos] || []).slice().sort(function (a, b) { return S.ratings[b.key] - S.ratings[a.key]; });
    var rows = "", lastTier = null;
    pool.forEach(function (e, i) {
      var t = tiers[e.key];
      if (t !== lastTier) {
        lastTier = t;
        var note = (S.notes[pos] || {})[t] || "";
        rows += "<tr class='tier-head'><td colspan='6'><span class='tier'>" + t + "</span> " +
          "<input class='note-input' placeholder='Describe this tier (shows on your packet)…' " +
          "value=\"" + esc(note) + "\" " +
          "oninput=\"FF.setNote('" + pos + "'," + t + ",this.value)\"></td></tr>";
      }
      rows += "<tr><td>" + (i + 1) + "</td><td>" + esc(e.name) + (e.rookie ? " <span class='badge'>R</span>" : "") +
        "</td><td>" + esc(e.team) + "</td><td>" + Math.round(S.ratings[e.key]) +
        "</td><td><span class='tier'>" + t + "</span></td><td>" + S.comps[e.key] + "</td></tr>";
    });
    app.innerHTML = nav(" &middot; <a href='#/play/" + pos + "'>play " + pos + "</a>") +
      "<h1>" + pos + " ranking</h1><p class='lead'>Ordered by your user rating; tiers via k-means " +
      "on ratings. Name each tier &mdash; the notes become section labels on your draft packet.</p>" +
      "<div class='table-wrap'><table><thead><tr><th>#</th><th>Player</th><th>Tm</th><th>Rating</th><th>Tier</th><th>Picks</th></tr></thead>" +
      "<tbody>" + rows + "</tbody></table></div>";
  }

  function packet() {
    var D = window.FF_DATA || {};
    var secs = "";
    ORDER.forEach(function (pos) {
      var pool = (DATA[pos] || []).slice();
      if (!pool.length) return;
      var tiers = tiersFor(pos);
      pool.sort(function (a, b) {
        return (tiers[a.key] - tiers[b.key]) || (S.ratings[b.key] - S.ratings[a.key]);
      });
      var hasBkp = pos !== "DST";
      var shareCols = { RB: ["Tgt%", "Rush%"], WR: ["Tgt%", "Rush%"], TE: ["Tgt%"] }[pos] || [];
      var cols = 6 + shareCols.length + (hasBkp ? 3 : 0);
      var head = "<tr><th class='note-col'>Tier</th><th>Tm</th><th>PPG</th><th>Starter</th>" +
        shareCols.map(function (h) { return "<th>" + h + "</th>"; }).join("") +
        "<th>$</th><th>Bid</th>" +
        (hasBkp ? "<th>Bkp PPG</th><th>Backup</th><th>Bid</th>" : "") + "</tr>";
      var body = "", lastTier = null;
      pool.forEach(function (e) {
        var t = tiers[e.key], first = t !== lastTier;
        if (first && lastTier !== null) body += "<tr class='tier-gap'><td colspan='" + cols + "'></td></tr>";
        lastTier = t;
        var label = "";
        if (first) {
          label = (S.notes[pos] || {})[t] || "";
          if (!label) {
            var ps = pool.filter(function (x) { return tiers[x.key] === t && x.price != null; })
              .map(function (x) { return x.price; });
            label = "Tier " + t + (ps.length
              ? " — $" + Math.round(Math.min.apply(null, ps)) + "-" + Math.round(Math.max.apply(null, ps))
              : "");
          }
        }
        var c = e.cols || {};
        body += "<tr><td class='note-col'>" + esc(label) + "</td><td>" + esc(e.team) +
          "</td><td>" + (e.rookie ? "R" : Math.max(0, e.ppg)) + "</td><td class='pk-name'>" + esc(e.name) +
          "</td>" +
          shareCols.map(function (h) { return "<td>" + esc(c[h] == null ? "" : c[h]) + "</td>"; }).join("") +
          "<td>" + (e.price != null ? "$" + Math.round(e.price) : "") + "</td><td class='bid'></td>" +
          (hasBkp
            ? "<td>" + (e.bkp_ppg != null && e.bkp_ppg !== "" ? e.bkp_ppg : "") + "</td><td>" +
              esc(e.bkp || "") + "</td><td class='bid'></td>"
            : "") +
          "</tr>";
      });
      secs += "<section class='pk-sec'><h2>" + pos + "</h2><div class='table-wrap'>" +
        "<table class='pk'><thead>" + head + "</thead><tbody>" + body + "</tbody></table></div></section>";
    });

    var extra = "";
    if (D.teams && D.teams.length) {
      var v = function (x) { return x == null ? "" : x; };
      var trs = D.teams.map(function (t, i) {
        return "<tr><td>" + (i + 1) + "</td><td>" + esc(t.team) + "</td><td class='pk-name'>" + coachName(t.hc, t.hcN) +
          "</td><td class='pk-name'>" + coachName(t.oc, t.ocN) + "</td><td>" + v(t.pf) + "</td><td>" + v(t.pa) +
          "</td><td>" + v(t.pag) + "</td><td>" + v(t.yds) + "</td><td>" + v(t.ydsg) +
          "</td><td>" + v(t.plays) + "</td><td>" + v(t.ypp) +
          "</td><td>" + v(t.pass) + "</td><td>" + v(t.passAtt) + "</td><td>" + v(t.passRk) +
          "</td><td>" + v(t.rush) + "</td><td>" + v(t.rushAtt) + "</td><td>" + v(t.rushRk) +
          "</td><td>" + v(t.td) + "</td><td>" + v(t.patd) + "</td><td>" + v(t.rutd) +
          "</td><td>" + v(t.vacTgt) + "</td><td>" + v(t.vacRush) +
          "</td><td>" + esc(t.qb) + "</td><td>" + esc(t.rb) + "</td><td>" + esc(v(t.rb2)) +
          "</td><td>" + esc(t.wr1) + "</td><td>" + esc(t.wr2) + "</td><td>" + esc(t.wr3) +
          "</td><td>" + esc(t.te) + "</td></tr>";
      }).join("");
      extra += "<section class='pk-sec'><h2>Team Stats</h2><div class='table-wrap'>" +
        "<table class='pk'><thead><tr><th>Rk</th><th>Tm</th><th>HC</th><th>OC</th><th>PF</th>" +
        "<th>PA</th><th>PA/G</th><th>Yds</th><th>Yds/G</th><th>Plays</th><th>Y/P</th>" +
        "<th>Pass</th><th>Att</th><th>Rk</th><th>Rush</th><th>Att</th><th>Rk</th>" +
        "<th>TD</th><th>PaTD</th><th>RuTD</th><th>VacTgt%</th><th>VacRush%</th>" +
        "<th>QB</th><th>RB1</th><th>RB2</th><th>WR1</th><th>WR2</th><th>WR3</th><th>TE</th></tr></thead>" +
        "<tbody>" + trs + "</tbody></table></div></section>";
    }
    if (D.top200 && D.top200.length) {
      var hs = (D.top200_headers || []).map(function (h) { return "<th>" + esc(h) + "</th>"; }).join("");
      var rs = D.top200.map(function (row, i) {
        return "<tr><td>" + (i + 1) + "</td>" + row.map(function (v, j) {
          return "<td" + (j === 0 ? " class='pk-name'" : "") + ">" + esc(v) + "</td>";
        }).join("") + "</tr>";
      }).join("");
      extra += "<section class='pk-sec'><h2>Top 200</h2><div class='table-wrap'>" +
        "<table class='pk'><thead><tr><th>#</th>" + hs + "</tr></thead><tbody>" + rs +
        "</tbody></table></div></section>";
    }
    if (!extra) {
      extra = "<p class='muted'>Team Stats / Top 200 need a regenerated data.js " +
        "(run the Rebuild Master Tiers action).</p>";
    }

    app.innerHTML = nav(" &middot; <span class='muted'>my packet</span>") +
      "<div class='no-print'><h1>My draft packet</h1><p class='lead'>Built from your ratings and " +
      "tier notes. Print it (or save as PDF) and write bids in the blank columns.</p>" +
      "<div class='actions'><button class='btn btn-primary' onclick='window.print()'>&#128424; Print / Save PDF</button></div></div>" +
      "<div class='packet'>" + secs + extra + "</div>";
  }

  // ---- public actions ----
  window.FF = {
    setNote: function (pos, tier, text) {
      S.notes[pos] = S.notes[pos] || {};
      text = String(text || "").slice(0, 200);
      if (text) S.notes[pos][tier] = text; else delete S.notes[pos][tier];
      save(S);
    },
    choose: function (winner, loser, pos) { pick(winner, loser); play(pos); },
    again: function (pos) { play(pos); },
    noPick: function (a, b, pos) {
      // Neither interests you: drop both presented options (-10 Elo each),
      // count as a comparison for both, then show a new pair.
      S.ratings[a] -= NUDGE; S.ratings[b] -= NUDGE;
      S.comps[a]++; S.comps[b]++;
      save(S); play(pos);
    },
    commitPicks: function () {
      // Push just the players you moved (rating != seed) to the repo through the
      // serverless proxy, which commits picks/u-<id>.csv on your behalf - no
      // popup, no download, no token in the browser. Each browser has a stable
      // id, so re-submitting overwrites your one file (one vote each). Run
      // "Rebuild Master Tiers" afterward to fold every user's file in.
      var rows = [];
      ALL.forEach(function (e) {
        if (Math.abs((S.ratings[e.key] || 0) - e.seed) > 0.001) {
          rows.push(e.key + "," + (Math.round(S.ratings[e.key] * 100) / 100));
        }
      });
      if (!rows.length) { alert("No picks to save yet - play a few matchups first."); return; }
      if (!WORKER_URL) {
        alert("Commit isn't wired up yet: no Worker URL is set.\n" +
              "See infra/commit-worker/README.md to deploy the proxy and set it.");
        return;
      }
      var csv = "key,rating\n" + rows.join("\n") + "\n";
      var id = userId();
      var btn = document.querySelector(".btn-primary");
      if (btn) { btn.disabled = true; btn.textContent = "Saving…"; }
      var done = function () { if (btn) { btn.disabled = false; btn.innerHTML = "&#128640; Commit to GitHub"; } };
      fetch(WORKER_URL, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id: id, csv: csv })
      }).then(function (r) {
        return r.json().catch(function () { return {}; }).then(function (j) {
          if (!r.ok) throw new Error(j.error || ("HTTP " + r.status));
          return j;
        });
      }).then(function () {
        done();
        alert("Saved your rankings to GitHub (id " + id + ", " + rows.length +
              " players).\nRun Rebuild Master Tiers to fold everyone in.");
      }).catch(function (err) {
        done();
        alert("Couldn't save to GitHub: " + err.message);
      });
    },
    exportTiers: function () {
      // key + manual_tier first (so the CLI/Action can read it), then human
      // columns (name/pos/rookie) so you can edit it by hand in a spreadsheet.
      // tier_note repeats your tier description on each row of the tier, the
      // same layout the master CSV uses, so notes survive the round trip.
      var q = function (s) { return '"' + String(s == null ? "" : s).replace(/"/g, '""') + '"'; };
      var HEADERS = (window.FF_DATA && window.FF_DATA.stat_headers) || [];
      var lines = [["key,manual_tier,rating,tier_note,name,pos,team,rookie,total,ppg"].concat(HEADERS).join(",")];
      ORDER.forEach(function (p) {
        var t = tiersFor(p);
        var pool = (DATA[p] || []).slice().sort(function (a, b) {
          return (t[a.key] - t[b.key]) || (S.ratings[b.key] - S.ratings[a.key]);
        });
        pool.forEach(function (e) {
          var c = e.cols || {};
          // rating is the continuous user rating the rebuild action averages.
          var row = [e.key, t[e.key], Math.round(S.ratings[e.key] * 100) / 100,
                     q((S.notes[p] || {})[t[e.key]] || ""),
                     q(e.name), e.pos, e.team || "", e.rookie ? 1 : 0, e.total, e.ppg];
          HEADERS.forEach(function (h) { row.push(c[h] == null ? "" : c[h]); });
          lines.push(row.join(","));
        });
      });
      var blob = new Blob([lines.join("\n") + "\n"], { type: "text/csv" });
      var url = URL.createObjectURL(blob), a = document.createElement("a");
      a.href = url; a.download = "app_tiers.csv"; a.click(); URL.revokeObjectURL(url);
    },
    importTiers: function (input) {
      var file = input.files && input.files[0];
      if (!file) return;
      // Quote-aware CSV field split (notes/names may contain commas).
      var splitCsv = function (line) {
        var out = [], cur = "", inQ = false;
        for (var i = 0; i < line.length; i++) {
          var ch = line[i];
          if (inQ) {
            if (ch === '"' && line[i + 1] === '"') { cur += '"'; i++; }
            else if (ch === '"') { inQ = false; }
            else cur += ch;
          } else if (ch === '"') { inQ = true; }
          else if (ch === ",") { out.push(cur); cur = ""; }
          else cur += ch;
        }
        out.push(cur);
        return out;
      };
      var reader = new FileReader();
      reader.onload = function () {
        var lines = String(reader.result).split(/\r?\n/).filter(function (l) { return l.trim(); });
        // Header-aware: find the key / rating / manual_tier columns by name.
        var hasHeader = /^\s*key\s*,/i.test(lines[0]);
        var cols = hasHeader ? splitCsv(lines[0]).map(function (s) { return s.trim().toLowerCase(); }) : [];
        var iKey = hasHeader ? cols.indexOf("key") : 0;
        var iRating = hasHeader ? cols.indexOf("rating") : -1;
        var iTier = hasHeader ? cols.indexOf("manual_tier") : 1;
        var iNote = hasHeader ? cols.indexOf("tier_note") : -1;
        var iPos = hasHeader ? cols.indexOf("pos") : -1;
        var n = 0;
        for (var i = hasHeader ? 1 : 0; i < lines.length; i++) {
          var parts = splitCsv(lines[i]);
          var key = (parts[iKey] || "").trim();
          if (!key || !(key in S.ratings)) continue;
          var rating = iRating >= 0 ? parseFloat(parts[iRating]) : NaN;
          var tier = parseInt(parts[iTier], 10);
          if (!isNaN(rating)) {
            S.ratings[key] = rating;            // continuous master rating: use as-is
          } else {
            if (!tier) continue;
            var e = BYKEY[key];                 // legacy tiers-only file: anchor by tier
            S.ratings[key] = (8 - tier) * 40 + (e ? e.seed * 0.001 : 0);
          }
          // Bring tier notes along (keyed by the file's pos + tier).
          if (iNote >= 0 && iPos >= 0 && tier) {
            var note = (parts[iNote] || "").trim();
            var pos = (parts[iPos] || "").trim().toUpperCase();
            if (note && pos) {
              S.notes[pos] = S.notes[pos] || {};
              if (!S.notes[pos][tier]) S.notes[pos][tier] = note.slice(0, 200);
            }
          }
          n++;
        }
        save(S);
        alert("Imported " + n + " tiers. Your ranking now reflects the file.");
        route();
      };
      reader.readAsText(file);
    }
  };

  function route() {
    var h = location.hash || "#/";
    var m = h.match(/^#\/play\/(\w+)/); if (m) return play(m[1]);
    m = h.match(/^#\/rank\/(\w+)/); if (m) return rank(m[1]);
    if (h.indexOf("#/packet") === 0) return packet();
    home();
  }
  window.addEventListener("hashchange", route);
  if (!ALL.length) { app.innerHTML = "<p>No player data found. Run <code>build-webapp</code> to generate data.js.</p>"; }
  else route();
})();
