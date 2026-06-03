/* FF Tier Builder - static head-to-head pick game.
   Seeds an in-browser Elo from the computed auction value, refines it with your
   A/B picks, derives tiers, and exports a tiers CSV you can feed back into the
   valuation CLI. All state lives in localStorage - no server. */
(function () {
  "use strict";

  var DATA = (window.FF_DATA || { positions: {} }).positions;
  var ORDER = ["QB", "RB", "WR", "TE", "K", "DST"];
  var TIER_K = { QB: 6, RB: 8, WR: 8, TE: 6, K: 5, DST: 6 };
  var SCALE = 400, K = 24, NEAR = 4;
  var STORE = "ff_tier_state_v1";

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
    ALL.forEach(function (e) {              // seed any missing
      if (s.ratings[e.key] == null) s.ratings[e.key] = e.seed;
      if (s.comps[e.key] == null) s.comps[e.key] = 0;
    });
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

  function matchup(pos) {
    var pool = (DATA[pos] || []).slice();
    if (pool.length < 2) return null;
    var fewest = Math.min.apply(null, pool.map(function (e) { return S.comps[e.key]; }));
    var cands = pool.filter(function (e) { return S.comps[e.key] <= fewest + 1; });
    var a = cands[Math.floor(Math.random() * cands.length)];
    var others = pool.filter(function (e) { return e.key !== a.key; })
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
  function tiersFor(pos) {
    var pool = (DATA[pos] || []).slice().sort(function (a, b) { return S.ratings[b.key] - S.ratings[a.key]; });
    var labels = kmeans1d(pool.map(function (e) { return S.ratings[e.key]; }), TIER_K[pos] || 6);
    var out = {}; pool.forEach(function (e, i) { out[e.key] = labels[i]; }); return out;
  }

  // ---- views ----
  var app = document.getElementById("app");
  function esc(s) { return String(s == null ? "" : s).replace(/[&<>"]/g, function (c) { return ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]; }); }
  function nav(extra) {
    return "<div class='nav'><a href='#/'>&#127944; Home</a>" + (extra || "") +
      "<span class='spacer'></span><span class='muted'>" + S.picks + " picks</span>" +
      "<button class='btn' onclick='FF.reset()'>Reset</button></div>";
  }

  function home() {
    var g = ORDER.filter(function (p) { return (DATA[p] || []).length; }).map(function (p) {
      var picks = (DATA[p] || []).reduce(function (a, e) { return a + S.comps[e.key]; }, 0);
      return "<button onclick=\"location.hash='#/play/" + p + "'\"><b>" + p + "</b><br>" +
        "<span class='muted'>" + Math.round(picks / 2) + " picks &middot; play &raquo;</span></button>";
    }).join("");
    app.innerHTML = nav() +
      "<h1>Tier Builder</h1><p class='muted'>Pick who you'd rather draft. Your picks " +
      "become user ratings &rarr; tiers. Export to edit by hand, import to load it back.</p>" +
      "<div class='pos-grid'>" + g + "</div>" +
      "<p style='margin-top:1rem'>" +
      "<button class='btn' onclick='FF.exportTiers()'>&#11015; Export tiers CSV</button> " +
      "<label class='btn' style='cursor:pointer'>&#11014; Import tiers CSV" +
      "<input type='file' accept='.csv' style='display:none' onchange='FF.importTiers(this)'></label>" +
      "</p>";
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
      "<div class='muted'>HC " + esc(e.hc || "TBD") + " &middot; OC " + esc(e.oc || "TBD") +
      " &middot; PC " + esc(e.pc || "TBD") + "</div>" + draftLine +
      "<div class='row'><span>Last-yr total</span><b>" + stat(e.total) + "</b></div>" +
      "<div class='row'><span>Last-yr PPG</span><b>" + stat(e.ppg) + "</b></div>" +
      "<div class='row'><span>3-yr weighted</span><b>" + stat(e.w3yr) + "</b></div>" +
      (e.stat ? "<div class='muted'>" + esc(e.stat) + "</div>" : "") +
      (e.tmoff ? "<div class='muted'>" + esc(e.tmoff) + "</div>" : "");
  }

  function play(pos) {
    var m = matchup(pos);
    if (!m) { app.innerHTML = nav() + "<h1>" + pos + "</h1><p>Not enough " + pos + " players.</p>"; return; }
    var a = m[0], b = m[1];
    app.innerHTML = nav(" &middot; <a href='#/rank/" + pos + "'>" + pos + " ranking</a>") +
      "<h1>" + pos + " &mdash; who'd you rather?</h1>" +
      "<div class='cards'>" +
        "<button class='card' onclick=\"FF.choose('" + a.key + "','" + b.key + "','" + pos + "')\">" + statBlock(a) + "</button>" +
        "<button class='card' onclick=\"FF.choose('" + b.key + "','" + a.key + "','" + pos + "')\">" + statBlock(b) + "</button>" +
      "</div><p class='vs'><a href='#/play/" + pos + "'>&#8635; different pair</a></p>";
  }

  function rank(pos) {
    var tiers = tiersFor(pos);
    var pool = (DATA[pos] || []).slice().sort(function (a, b) { return S.ratings[b.key] - S.ratings[a.key]; });
    var rows = pool.map(function (e, i) {
      return "<tr><td>" + (i + 1) + "</td><td>" + esc(e.name) + (e.rookie ? " (R)" : "") +
        "</td><td>" + esc(e.team) + "</td><td>" + Math.round(S.ratings[e.key]) +
        "</td><td>" + tiers[e.key] + "</td><td>" + S.comps[e.key] + "</td></tr>";
    }).join("");
    app.innerHTML = nav(" &middot; <a href='#/play/" + pos + "'>play " + pos + "</a>") +
      "<h1>" + pos + " ranking</h1><p class='muted'>Ordered by your user rating; tiers via k-means on ratings.</p>" +
      "<table><tr><th>#</th><th>Player</th><th>Tm</th><th>Rating</th><th>Tier</th><th>Picks</th></tr>" + rows + "</table>";
  }

  // ---- public actions ----
  window.FF = {
    choose: function (winner, loser, pos) { pick(winner, loser); play(pos); },
    reset: function () { if (confirm("Clear all picks and ratings?")) { localStorage.removeItem(STORE); S = load(); route(); } },
    exportTiers: function () {
      // key + manual_tier first (so the CLI/Action can read it), then human
      // columns (name/pos/rookie) so you can edit it by hand in a spreadsheet.
      var q = function (s) { return '"' + String(s == null ? "" : s).replace(/"/g, '""') + '"'; };
      var HEADERS = (window.FF_DATA && window.FF_DATA.stat_headers) || [];
      var lines = [["key,manual_tier,name,pos,rookie,total,ppg"].concat(HEADERS).join(",")];
      ORDER.forEach(function (p) {
        var t = tiersFor(p);
        var pool = (DATA[p] || []).slice().sort(function (a, b) {
          return (t[a.key] - t[b.key]) || (S.ratings[b.key] - S.ratings[a.key]);
        });
        pool.forEach(function (e) {
          var c = e.cols || {};
          var row = [e.key, t[e.key], q(e.name), e.pos, e.rookie ? 1 : 0, e.total, e.ppg];
          HEADERS.forEach(function (h) { row.push(c[h] == null ? "" : c[h]); });
          lines.push(row.join(","));
        });
      });
      var blob = new Blob([lines.join("\n") + "\n"], { type: "text/csv" });
      var url = URL.createObjectURL(blob), a = document.createElement("a");
      a.href = url; a.download = "tiers.csv"; a.click(); URL.revokeObjectURL(url);
    },
    importTiers: function (input) {
      var file = input.files && input.files[0];
      if (!file) return;
      var reader = new FileReader();
      reader.onload = function () {
        var lines = String(reader.result).split(/\r?\n/).filter(function (l) { return l.trim(); });
        var start = /^\s*key\s*,/i.test(lines[0]) ? 1 : 0;  // skip header if present
        var n = 0;
        for (var i = start; i < lines.length; i++) {
          var parts = lines[i].split(",");
          var key = (parts[0] || "").trim();
          var tier = parseInt(parts[1], 10);
          if (!key || !tier || !(key in S.ratings)) continue;
          var e = BYKEY[key];
          // Anchor rating by the imported tier (value as a within-tier tiebreak).
          S.ratings[key] = (8 - tier) * 100 + (e ? Math.min(e.seed, 99) * 0.001 : 0);
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
    home();
  }
  window.addEventListener("hashchange", route);
  if (!ALL.length) { app.innerHTML = "<p>No player data found. Run <code>build-webapp</code> to generate data.js.</p>"; }
  else route();
})();
