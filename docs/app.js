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
  // Optional private-league passcode. PUBLIC mode (no LEAGUE_CODE on the
  // Worker) never prompts anyone; the app only asks if the server answers 401,
  // then remembers the code locally.
  var CODE_STORE = "ff_league_code";
  // Optional Cloudflare Turnstile bot check: activates when the site config
  // carries a sitekey AND the Worker has TURNSTILE_SECRET. Invisible to most
  // humans; blocks headless bot spam on the public endpoint.
  var TS_KEY = (window.FF_CONFIG && window.FF_CONFIG.turnstileSiteKey) || "";
  var tsToken = "", tsWidget = null;
  if (TS_KEY) {
    window.__ffTs = function () {
      var div = document.createElement("div");
      div.style.cssText = "position:fixed;bottom:8px;right:8px;z-index:50";
      document.body.appendChild(div);
      tsWidget = window.turnstile.render(div, {
        sitekey: TS_KEY,
        callback: function (t) { tsToken = t; },
        "expired-callback": function () { tsToken = ""; window.turnstile.reset(tsWidget); }
      });
    };
    var tss = document.createElement("script");
    tss.src = "https://challenges.cloudflare.com/turnstile/v0/api.js?onload=__ffTs";
    tss.async = true;
    document.head.appendChild(tss);
  }
  function userId() {
    var id = localStorage.getItem(UID_STORE);
    if (!id) {
      id = Date.now().toString(36) + Math.random().toString(36).slice(2, 8);
      localStorage.setItem(UID_STORE, id);
    }
    return id;
  }
  // ---- haptics ----
  // Vibration API: buzzes on Android (Chrome/Firefox); iOS Safari and desktops
  // have no navigator.vibrate, so this is a silent no-op there.
  var BUZZ = {
    pick: 12,                     // light tick: a matchup decision landed
    fade: [12, 60, 12],           // double tap: faded both players
    saved: [25, 50, 25, 50, 90],  // rising triple: rankings committed
    error: [90, 60, 90]           // heavy double: commit failed
  };
  function buzz(pattern) {
    try { if (navigator.vibrate) navigator.vibrate(pattern); } catch (e) {}
  }
  // Visual counterpart, for platforms with no vibration (iOS Safari): the
  // chosen card pops (or a faded pair sinks) for PICK_MS before the next
  // matchup renders; duelLock swallows taps during that beat so a double-tap
  // can't record a second pick. flashBtn pulses/shakes the commit button.
  var PICK_MS = 160, duelLock = false;
  function flashBtn(cls) {
    var btn = document.querySelector(".btn-primary");
    if (!btn) return;
    btn.classList.remove("flash-ok", "flash-err");
    void btn.offsetWidth;               // restart the animation if re-flashed
    btn.classList.add(cls);
    setTimeout(function () { btn.classList.remove(cls); }, 700);
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
    s.fades = s.fades || {};   // {key: fade-both count this master}
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
    // Master refresh: when a rebuild ships a new master (the base stamp
    // changes), the WHOLE board re-seeds from it. Committed picks are
    // already blended into that master - the new consensus (crowd + admin
    // tiers) is the board everyone refines next. Pick counts, trophies and
    // tier notes survive; only the ratings snap to the new master.
    // The "r2:" prefix versions the re-seed LOGIC itself: browsers that
    // stored a bare base under the old partial-re-seed code migrate through
    // one full re-seed even though the master hasn't changed since.
    var dataBase = "r2:" + String((window.FF_DATA || {}).base || "");
    if (s.base !== dataBase) {
      ALL.forEach(function (e) { s.ratings[e.key] = e.seed; });
      s.fades = {};            // benched players return with the new master
      s.base = dataBase;
      healed = true;
    }
    if (healed) localStorage.setItem(STORE, JSON.stringify(s));
    return s;
  }
  function save(s) { localStorage.setItem(STORE, JSON.stringify(s)); }
  var S = load();

  // ---- elo ----
  function expected(a, b) { return 1 / (1 + Math.pow(10, (b - a) / SCALE)); }
  function pick(winner, loser) {
    buzz(BUZZ.pick);
    var rw = S.ratings[winner], rl = S.ratings[loser];
    var e = expected(rw, rl);
    S.ratings[winner] = rw + K * (1 - e);
    S.ratings[loser] = rl + K * (0 - (1 - e));
    S.comps[winner]++; S.comps[loser]++; S.picks++;
    save(S);
    checkTrophies();
  }

  var lastPair = [];
  var FADE_BENCH = 5;  // fade a player this many times: benched until next master
  function matchup(pos, avoid) {
    avoid = avoid || [];
    var pool = (DATA[pos] || []).slice();
    if (pool.length < 2) return null;
    // Benched = faded FADE_BENCH+ times since the current master shipped.
    var fresh = pool.filter(function (e) { return (S.fades[e.key] || 0) < FADE_BENCH; });
    if (fresh.length < 2) fresh = pool;  // nearly-all-benched pool: relax
    var elig = fresh.filter(function (e) { return avoid.indexOf(e.key) < 0; });
    if (elig.length < 2) elig = fresh;  // tiny pool: can't avoid
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
    thr -= 1e-6;  // float-jitter tolerance for by-construction-equal gaps
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
    // Master-anchored bands when data.js carries the master's tiers: the
    // boundaries come from the master's (tier, seed) geometry and each player
    // slots by YOUR rating - fresh state shows exactly the master's tiers
    // (admin pins included, all of them, not capped at k), and your picks
    // move players across boundaries as your ratings drift.
    if (pool.length && pool.every(function (e) { return e.tier; })) {
      var mast = pool.slice().sort(function (a, b) {
        return (a.tier - b.tier) || (b.seed - a.seed);
      });
      var bounds = [];
      for (var i = 0; i < mast.length - 1; i++) {
        if (mast[i].tier !== mast[i + 1].tier) {
          bounds.push((mast[i].seed + mast[i + 1].seed) / 2);
        }
      }
      var out = {};
      pool.forEach(function (e) {
        var r = S.ratings[e.key], t = 1;
        for (var j = 0; j < bounds.length; j++) { if (r < bounds[j]) t++; }
        out[e.key] = t;
      });
      return out;
    }
    // Old data.js without per-entity tiers: derive from rating gaps.
    var labels = sizedTiers(pool.map(function (e) { return S.ratings[e.key]; }), TIER_K[pos] || 6, 7);
    var out2 = {}; pool.forEach(function (e, i) { out2[e.key] = labels[i]; }); return out2;
  }

  // ---- levels / gamification ----
  var LEADERS = (window.FF_DATA || {}).leaders || {};
  var LEVELS = [
    { name: "NOOB", won: "First snap taken - the training wheels are officially game-worn.", emoji: "\ud83c\udf7c", min: 0,
      req: "under 200 picks", img: "img/trophy-noob.webp",
      smack: "Everyone starts somewhere. Right now your mock drafts are mocking you." },
    { name: "Scrub", won: "Promoted from waterboy. You now carry the clipboard with authority.", emoji: "\ud83e\uddfd", min: 200,
      req: "200+ picks", img: "img/trophy-scrub2.webp",
      smack: "You can tell a sleeper from a bust... barely. Keep clicking." },
    { name: "Taco", won: "Taco unlocked - crunchy on the outside, pure upside on the inside.", emoji: "\ud83c\udf2e", min: 500,
      req: "500+ picks", img: "img/trophy-taco.webp",
      smack: "You're the league taco - delicious, and everybody wants a bite of your matchup." },
    { name: "Middle of League", won: "Gloriously average! The playoff bubble has a seat with your name on it.", emoji: "\ud83d\ude10", min: 1000,
      req: "1,000+ picks", img: "img/trophy-mol.webp",
      smack: "Respectably mediocre. The playoff bubble is your natural habitat." },
    { name: "Division Bully", won: "The division group chat just went quiet. They know.", emoji: "\ud83d\ude24", min: 2000,
      req: "2,000+ picks", img: "img/trophy-bully.webp",
      smack: "You're out here stealing lunch money from your division rivals." },
    { name: "Contender", won: "Vegas moved your odds. The whole room watches your nominations now.", emoji: "\ud83d\udd25", min: 3500,
      req: "3,500+ picks", img: "img/trophy-contender.webp",
      smack: "The room goes quiet when you nominate. One more push." },
    { name: "Super Bowl Player", won: "Confetti in your hair - two full rosters of drafters look up at you.", emoji: "\u2b50", min: 5000, rank: 106,
      req: "5,000+ picks & top 106 all-time", img: "img/trophy-sbp.webp",
      smack: "Top 106 in the world - that's a full two-deep NFL roster of drafters, and you made the trip." },
    { name: "League Champ", won: "Ring sized. Banner hung. Trash talk immortalized. Bow to the Champ.", emoji: "\ud83c\udfc6", min: 5000, rank: 11,
      req: "5,000+ picks & top 11 all-time", img: "img/trophy-champ.webp",
      smack: "Top 11. You ARE the starting lineup. Everyone else is drafting for second." }
  ];
  function myTotalPicks() {
    var mine = LEADERS[localStorage.getItem(UID_STORE)] || 0;
    return Math.max(S.picks || 0, mine);
  }
  function myRank() {
    // 1 + committers strictly ahead of me, all-time. 0 = not on the board yet.
    var me = localStorage.getItem(UID_STORE);
    if (!me || LEADERS[me] == null) return 0;
    var mine = myTotalPicks(), ahead = 0, uid;
    for (uid in LEADERS) { if (uid !== me && LEADERS[uid] > mine) ahead++; }
    return 1 + ahead;
  }
  function levelIndex() {
    var picks = myTotalPicks(), rank = myRank(), i;
    for (i = LEVELS.length - 1; i >= 0; i--) {
      var lv = LEVELS[i];
      if (picks < lv.min) continue;
      if (lv.rank && (rank === 0 || rank > lv.rank)) continue;
      return i;
    }
    return 0;
  }
  function checkTrophies() {
    S.trophies = S.trophies || {};
    var idx = levelIndex(), changed = false, i;
    for (i = 0; i <= idx; i++) {
      if (!S.trophies[LEVELS[i].name]) { S.trophies[LEVELS[i].name] = Date.now(); changed = true; }
    }
    if (changed) save(S);
  }
  checkTrophies();
  function cheerleaderSvg() {
    // Hand-drawn pom-pom cheerleader in the app palette - no licensed assets.
    return "<svg viewBox='0 0 64 64' width='56' height='56' aria-hidden='true'>" +
      "<g stroke='var(--accent-ink)' stroke-width='2.4' stroke-linecap='round' fill='none'>" +
      "<line x1='32' y1='30' x2='32' y2='44'/>" +               // torso
      "<line x1='32' y1='33' x2='18' y2='20'/>" +               // arms up
      "<line x1='32' y1='33' x2='46' y2='20'/>" +
      "<line x1='27' y1='55' x2='30' y2='47'/>" +               // legs
      "<line x1='39' y1='56' x2='34' y2='47'/>" +
      "</g>" +
      "<circle cx='32' cy='23' r='5.5' fill='var(--accent)'/>" +           // head
      "<path d='M24 44 L40 44 L44 52 L20 52 Z' fill='var(--accent)'/>" +   // skirt
      "<g fill='var(--down)'>" +
      "<circle cx='16' cy='18' r='5'/><circle cx='48' cy='18' r='5'/>" +   // pom-poms
      "</g>" +
      "<g stroke='var(--down)' stroke-width='1.4'>" +
      "<line x1='12' y1='13' x2='14' y2='15'/><line x1='20' y1='13' x2='18' y2='15'/>" +
      "<line x1='44' y1='13' x2='46' y2='15'/><line x1='52' y1='13' x2='50' y2='15'/>" +
      "</g></svg>";
  }

  // ---- views ----
  var app = document.getElementById("app");
  function esc(s) { return String(s == null ? "" : s).replace(/[&<>"]/g, function (c) { return ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]; }); }
  function nav(extra) {
    return "<div class='nav'><a class='brand' href='#/'><span class='ball'>&#127944;</span> Tier Builder</a>" + (extra || "") +
      "<span class='spacer'></span><a class='pill lv-pill' href='#/levels'>" +
      LEVELS[levelIndex()].emoji + " " + esc(LEVELS[levelIndex()].name) +
      " &middot; " + myTotalPicks() + "</a></div>";
  }

  function home() {
    var g = ORDER.filter(function (p) { return (DATA[p] || []).length; }).map(function (p) {
      var picks = (DATA[p] || []).reduce(function (a, e) { return a + S.comps[e.key]; }, 0);
      return "<button onclick=\"location.hash='#/play/" + p + "'\">" +
        "<span class='pos'>" + p + "</span>" +
        "<span class='sub'>" + Math.round(picks / 2) + " picks &middot; play &raquo;</span></button>";
    }).join("");
    app.innerHTML = nav() +
      "<p class='lead'>Pick who you'd rather draft. Your picks &rarr; ratings &rarr; tiers. " +
      "Export to save your picks and import on another device.</p>" +
      "<div class='pos-grid'>" + g + "</div>" +
      "<div class='actions'>" +
      "<button class='btn btn-primary' onclick='FF.commitPicks()'>&#128640; Commit to GitHub</button>" +
      "<button class='btn' onclick=\"location.hash='#/packet'\">&#128424; My draft packet</button>" +
      "<button class='btn' onclick='FF.exportTiers()'>&#11015; Export tiers CSV</button>" +
      "<label class='btn' style='cursor:pointer'>&#11014; Import tiers CSV" +
      "<input type='file' accept='.csv' style='display:none' onchange='FF.importTiers(this)'></label>" +
      "</div><p class='muted'>Commit sends your rankings to the shared database. " +
      "You can also download a draft packet of your tiers." +
      // Build stamp: which master this browser is actually looking at -
      // instantly settles "is my view stale or is the data wrong".
      "<br>Tiers build: <code>" +
      esc(String((window.FF_DATA || {}).base || "unstamped")) + "</code></p>";
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
      "<div class='vals'>" +
      "<span class='seg'><b>" + stat(e.total) + "</b><i>last yr</i></span>" +
      "<span class='seg'><b>" + stat(e.ppg) + "</b><i>ppg</i></span>" +
      "<span class='seg'><b>" + stat(e.w3yr) + "</b><i>3-yr wtd</i></span>" +
      "</div>" +
      statLine(e);
  }

  function play(pos) {
    var m = matchup(pos, lastPair);
    if (!m) { app.innerHTML = nav() + "<h1>" + pos + "</h1><p>Not enough " + pos + " players.</p>"; return; }
    var a = m[0], b = m[1];
    lastPair = [a.key, b.key];  // next pair will avoid these two
    app.innerHTML = "<div class='duel'>" +
      nav(" &middot; <a href='#/rank/" + pos + "'>" + pos + " ranking</a>") +
      "<h1>" + pos + " &mdash; who'd you rather?</h1>" +
      "<div class='cards'>" +
        "<button class='card' onclick=\"FF.choose('" + a.key + "','" + b.key + "','" + pos + "',this)\">" + statBlock(a) + "</button>" +
        "<button class='card' onclick=\"FF.choose('" + b.key + "','" + a.key + "','" + pos + "',this)\">" + statBlock(b) + "</button>" +
      "</div><p class='vs'>" +
        "<button class='btn' onclick=\"FF.again('" + pos + "')\">&#8635; skip</button> " +
        "<button class='btn' onclick=\"FF.noPick('" + a.key + "','" + b.key + "','" + pos + "')\">" +
        "&#8856; fade both</button>" +
      "</p></div>";
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
      var shareCols = { RB: ["Tgt%", "Rush%"], WR: ["Tgt%", "Rush%"], TE: ["Tgt%", "Rush%"] }[pos] || [];
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
    var statsTable = function (title, headers, rows) {
      var hs = (headers || []).map(function (h) { return "<th>" + esc(h) + "</th>"; }).join("");
      var rs = (rows || []).map(function (row, i) {
        return "<tr><td>" + (i + 1) + "</td>" + row.map(function (v, j) {
          return "<td" + (j === 0 ? " class='pk-name'" : "") + ">" + esc(v) + "</td>";
        }).join("") + "</tr>";
      }).join("");
      return "<section class='pk-sec'><h2>" + esc(title) + "</h2><div class='table-wrap'>" +
        "<table class='pk'><thead><tr><th>#</th>" + hs + "</tr></thead><tbody>" + rs +
        "</tbody></table></div></section>";
    };
    var yr = D.year ? D.year + " " : "";
    if (D.top200 && D.top200.length) {
      extra += statsTable(yr + "Top 200 Stats", D.top200_headers, D.top200);
    }
    if (D.qbstats && D.qbstats.length) {
      extra += statsTable(yr + "QB Stats", D.qb_headers, D.qbstats);
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

  function levelsPage() {
    checkTrophies();
    var idx = levelIndex(), picks = myTotalPicks(), rank = myRank();
    var committers = Object.keys(LEADERS).length;
    var rankLine = rank > 0
      ? "All-time rank <b>#" + rank + "</b> of " + committers + " committers"
      : "Not on the leaderboard yet - hit <b>Commit to GitHub</b> to get ranked" +
        (committers ? " (" + committers + " committers so far)" : "");

    // Champ at the top: render the ladder best-first (logic stays ascending).
    var ladder = LEVELS.map(function (lv, i) {
      var state = i < idx ? "lv-done" : (i === idx ? "lv-cur" : "lv-locked");
      var tag = i === idx ? "<span class='badge'>YOU ARE HERE</span>" :
                (i < idx ? "<span class='lv-check'>&#10003;</span>" : "");
      return "<div class='lv-card " + state + "'>" +
        "<span class='lv-emoji'>" + lv.emoji + "</span>" +
        "<div class='lv-body'><div class='lv-name'>" + esc(lv.name) + " " + tag + "</div>" +
        "<div class='muted lv-req'>" + esc(lv.req) + "</div>" +
        "<div class='lv-smack'>" + esc(lv.smack) + "</div></div></div>";
    }).reverse().join("");

    var trophies = LEVELS.map(function (lv) {
      var won = (S.trophies || {})[lv.name];
      if (won) {
        var art = lv.img
          ? "<img class='tr-img' src='" + lv.img + "' alt='' loading='lazy'>"
          : cheerleaderSvg();
        return "<div class='tr-card tr-won'>" + art +
          "<div class='lv-name'>" + lv.emoji + " " + esc(lv.name) + "</div>" +
          "<div class='muted'>" + esc(lv.won || ("Congratulations - you reached " + lv.name + "!")) + "</div></div>";
      }
      return "<div class='tr-card tr-locked'><span class='tr-lock'>&#128274;</span>" +
        "<div class='lv-name'>???</div>" +
        "<div class='muted'>" + esc(lv.req) + "</div></div>";
    }).reverse().join("");

    app.innerHTML = nav(" &middot; <span class='muted'>levels</span>") +
      "<h1>" + LEVELS[idx].emoji + " " + esc(LEVELS[idx].name) + "</h1>" +
      "<p class='lead'>" + picks + " lifetime picks &middot; " + rankLine + "</p>" +
      "<h2 class='lv-h2'>The ladder</h2>" +
      "<div class='lv-grid'>" + ladder + "</div>" +
      "<h2 class='lv-h2'>&#127942; Trophy case</h2>" +
      "<div class='tr-grid'>" + trophies + "</div>";
  }

  // ---- commissioner: master tier editor (#/admin) ----
  // The page is reachable by anyone (static site), but Overwrite only works
  // with the ADMIN_CODE secret held by the Worker. Tiers here are LITERAL:
  // every player carries an explicit tier assignment (no live re-derivation),
  // so any player can be put in any tier. Overwrite pushes the whole edited
  // position (order + tiers) and the rebuild pins it until released.
  var AD_STORE = "ff_admin_state";
  var AD = null, adSel = null;
  function adInit() {
    if (AD) return;
    try { AD = JSON.parse(localStorage.getItem(AD_STORE)); } catch (e) {}
    AD = AD || {};
    AD.ratings = AD.ratings || {};
    AD.tierOf = AD.tierOf || {};
    AD.changed = AD.changed || {};
    // After a rebuild (new base) with no unsaved edits, reload the board so
    // it reopens exactly as the new master stands. Unsaved edits are kept -
    // adminPage shows a stale note until they're pushed or reset.
    var dataBase = String((window.FF_DATA || {}).base || "");
    if (AD.base !== dataBase && !Object.keys(AD.changed).length) {
      AD = { ratings: {}, tierOf: {}, changed: {}, base: dataBase };
    }
    ALL.forEach(function (e) {
      if (AD.ratings[e.key] == null) AD.ratings[e.key] = e.seed;
    });
    ORDER.forEach(function (p) {
      var pool = DATA[p] || [];
      if (!pool.length || !pool.some(function (e) { return AD.tierOf[e.key] == null; })) return;
      // Seed the position's tiers: the master's own tier when data.js carries
      // it, else the same derivation the rebuild uses. One-time per position.
      if (pool.every(function (e) { return e.tier; })) {
        pool.forEach(function (e) { AD.tierOf[e.key] = e.tier; });
      } else {
        var sorted = pool.slice().sort(function (a, b) { return AD.ratings[b.key] - AD.ratings[a.key]; });
        var labels = sizedTiers(sorted.map(function (e) { return AD.ratings[e.key]; }), TIER_K[p] || 6, 7);
        sorted.forEach(function (e, i) { AD.tierOf[e.key] = labels[i]; });
      }
      adNorm(p);
    });
  }
  function adSave() { localStorage.setItem(AD_STORE, JSON.stringify(AD)); }
  function adPool(pos) {
    return (DATA[pos] || []).slice().sort(function (a, b) {
      return (AD.tierOf[a.key] - AD.tierOf[b.key]) ||
             (AD.ratings[b.key] - AD.ratings[a.key]) ||
             (a.name < b.name ? -1 : 1);
    });
  }
  function adNorm(pos) {
    // Renumber the position's tiers contiguous 1..K in board order, so merges,
    // splits and emptied tiers never leave gaps or fractional labels.
    var pool = adPool(pos), next = 0, lastLabel = null;
    pool.forEach(function (e) {
      var t = AD.tierOf[e.key];
      if (t !== lastLabel) { next++; lastLabel = t; }
      AD.tierOf[e.key] = next;
    });
  }
  function adPlace(pos, key, rating, tier) {
    AD.ratings[key] = rating;
    AD.tierOf[key] = tier;
    AD.changed[key] = 1;
    adNorm(pos);
    adSave();
    adminPage(pos);
  }
  function adMoveAbove(pos, key, targetKey) {
    if (key === targetKey) return;
    var list = adPool(pos).filter(function (e) { return e.key !== key; });
    var idx = -1;
    list.forEach(function (e, i) { if (e.key === targetKey) idx = i; });
    if (idx < 0) return;
    var below = AD.ratings[targetKey];
    var above = idx > 0 ? AD.ratings[list[idx - 1].key] : null;
    adPlace(pos, key, above === null ? below + 12 : (above + below) / 2,
            AD.tierOf[targetKey]);
  }
  function adMoveTierEnd(pos, key, tier) {
    var list = adPool(pos).filter(function (e) { return e.key !== key; });
    var last = -1;
    list.forEach(function (e, i) { if (AD.tierOf[e.key] <= tier) last = i; });
    if (last < 0) return;                      // empty target: nothing to anchor on
    var above = AD.ratings[list[last].key];
    var below = last + 1 < list.length ? AD.ratings[list[last + 1].key] : null;
    adPlace(pos, key, below === null ? above - 12 : (above + below) / 2, tier);
  }
  function adNewBottomTier(pos, key) {
    var maxTier = 0, minRating = Infinity;
    adPool(pos).forEach(function (e) {
      if (e.key === key) return;
      if (AD.tierOf[e.key] > maxTier) maxTier = AD.tierOf[e.key];
      if (AD.ratings[e.key] < minRating) minRating = AD.ratings[e.key];
    });
    adPlace(pos, key, (minRating === Infinity ? 100 : minRating) - 12, maxTier + 1);
  }
  function adSplitAt(pos, key) {
    // The selected player starts a brand-new tier: he and everyone below him
    // in his current tier drop into it together.
    var pool = adPool(pos), from = -1, tier = AD.tierOf[key];
    pool.forEach(function (e, i) { if (e.key === key) from = i; });
    if (from <= 0 || AD.tierOf[pool[from - 1].key] !== tier) return; // already a tier top
    for (var i = from; i < pool.length && AD.tierOf[pool[i].key] === tier; i++) {
      AD.tierOf[pool[i].key] = tier + 0.5;     // fractional; adNorm renumbers
      AD.changed[pool[i].key] = 1;
    }
    adNorm(pos);
    adSave();
    adminPage(pos);
  }
  function adMergeUp(pos, tier) {
    if (tier <= 1) return;
    adPool(pos).forEach(function (e) {
      if (AD.tierOf[e.key] === tier) { AD.tierOf[e.key] = tier - 1; AD.changed[e.key] = 1; }
    });
    adNorm(pos);
    adSave();
    adminPage(pos);
  }
  function adDirtyPositions() {
    var dirty = {};
    Object.keys(AD.changed).forEach(function (k) {
      var e = BYKEY[k];
      if (e) dirty[e.pos] = 1;
    });
    return Object.keys(dirty);
  }
  function adSend(code, csv, onOk) {
    fetch(WORKER_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ kind: "admin", code: code, csv: csv, ts: tsToken })
    }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (j) {
        if (!r.ok) throw new Error(j.error || ("HTTP " + r.status));
        return j;
      });
    }).then(function (j) {
      localStorage.setItem("ff_admin_code", code);
      onOk(j);
    }).catch(function (err) {
      if (String(err.message).indexOf("admin code") >= 0) {
        localStorage.removeItem("ff_admin_code");
        alert("Admin code rejected.");
      } else {
        alert("Overwrite failed: " + err.message);
      }
    });
  }

  // Pointer-based drag (grip handle): works with mouse AND touch, unlike
  // HTML5 drag-and-drop which never fires on phones. The grip captures the
  // pointer, a ghost chip follows it, and elementFromPoint picks the target.
  var adDrag = null, adSwallowClick = false;
  function adDropTarget(x, y) {
    var el = document.elementFromPoint(x, y);
    return el && el.closest ? el.closest(".ad-row,.ad-tier-head,.ad-newtier") : null;
  }
  function adClearOver() {
    var cur = document.querySelectorAll(".ad-over"), i;
    for (i = 0; i < cur.length; i++) cur[i].classList.remove("ad-over");
  }

  function adminPage(pos) {
    adInit();
    pos = pos || "RB";
    var pool = adPool(pos);
    var dirty = adDirtyPositions();

    var tabs = ORDER.filter(function (p) { return (DATA[p] || []).length; })
      .map(function (p) {
        var mark = dirty.indexOf(p) >= 0 ? "*" : "";
        return p === pos
          ? "<span class='tier'>" + p + mark + "</span>"
          : "<a href='#/admin/" + p + "'>" + p + mark + "</a>";
      }).join(" &middot; ");

    var counts = {};
    pool.forEach(function (e) { var t = AD.tierOf[e.key]; counts[t] = (counts[t] || 0) + 1; });

    var body = "", lastTier = null;
    pool.forEach(function (e, i) {
      var t = AD.tierOf[e.key];
      if (t !== lastTier) {
        lastTier = t;
        body += "<div class='ad-tier-head' data-tier='" + t + "' " +
          "onclick=\"FF.adClickTier('" + pos + "'," + t + ")\">" +
          "Tier " + t + " <span class='muted'>&middot; " + counts[t] +
          " &middot; tap = send here</span>" +
          (t > 1 ? "<button class='ad-mini' onclick=\"event.stopPropagation();" +
                   "FF.adMergeUp('" + pos + "'," + t + ")\">&#8963; merge up</button>" : "") +
          "</div>";
      }
      var sel = adSel === e.key;
      var canSplit = i > 0 && AD.tierOf[pool[i - 1].key] === t;
      var cls = "ad-row" + (AD.changed[e.key] ? " ad-chg" : "") + (sel ? " ad-sel" : "");
      body += "<div class='" + cls + "' data-key='" + e.key + "' " +
        "onclick=\"FF.adClickRow('" + pos + "','" + e.key + "')\">" +
        "<span class='ad-grip' " +
        "onpointerdown=\"FF.adGrip(event,'" + pos + "','" + e.key + "')\" " +
        "onpointermove='FF.adGripMove(event)' " +
        "onpointerup=\"FF.adGripUp(event,'" + pos + "')\" " +
        "onpointercancel='FF.adGripCancel()'>&#x2630;</span>" +
        "<b>" + esc(e.name) + "</b><span class='muted'> " + esc(e.team || "") +
        // Positional rank = board order; re-renders keep it live as you move
        // players, same as the rating.
        " &middot; " + pos + (i + 1) +
        " &middot; " + Math.round(AD.ratings[e.key]) + "</span>" +
        (sel ? "<span class='ad-actions'>" +
          (canSplit ? "<button class='ad-mini' onclick=\"event.stopPropagation();" +
                      "FF.adSplit('" + pos + "','" + e.key + "')\">&#9986; tier starts here</button>" : "") +
          "<button class='ad-mini' onclick='event.stopPropagation();FF.adDeselect()'>&#10005;</button>" +
          "</span>"
         : (AD.changed[e.key] ? "<span class='ad-dot'>&#9679;</span>" : "")) +
        "</div>";
    });
    body += "<div class='ad-newtier' data-newtier='1' " +
      "onclick=\"FF.adClickNew('" + pos + "')\">&#10133; drop or tap here = new bottom tier</div>";

    var stale = AD.base !== String((window.FF_DATA || {}).base || "");
    app.innerHTML = nav(" &middot; <span class='muted'>admin</span>") +
      "<h1>Commissioner tiers</h1>" +
      (stale ? "<p class='lead'>&#9888; These unsaved edits were started on an " +
       "OLDER master. Overwrite to push them anyway, or Reset edits to load " +
       "the latest master.</p>" : "") +
      "<p class='lead'>Drag the &#x2630; handle (works on touch), or tap a player then " +
      "tap where he goes. Tiers here are literal &mdash; exactly what you see is what " +
      "Overwrite pushes. Overwrite makes your board the master for the edited " +
      "position(s)" + (dirty.length ? " (<b>" + dirty.join(", ") + "</b>)" : "") +
      " and pins them until you release them back to the crowd.</p>" +
      "<p>" + tabs + "</p>" +
      "<div class='actions'>" +
      "<button class='btn btn-primary' onclick=\"FF.adOverwrite('" + pos + "')\">" +
      "&#9888; Overwrite master tiers</button>" +
      "<button class='btn' onclick=\"FF.adRelease('" + pos + "')\">Release " + pos + " to crowd</button>" +
      "<button class='btn' onclick='FF.adReset()'>Reset edits</button>" +
      "</div>" +
      "<div class='ad-list'>" + body + "</div>";
  }

  // ---- public actions ----

  window.FF = {
    adGrip: function (ev, pos, key) {
      ev.preventDefault();
      var g = document.createElement("div");
      g.className = "ad-ghost";
      g.textContent = (BYKEY[key] || {}).name || key;
      document.body.appendChild(g);
      g.style.left = (ev.clientX + 10) + "px";
      g.style.top = (ev.clientY - 14) + "px";
      adDrag = { key: key, pos: pos, ghost: g };
      if (ev.target.setPointerCapture) {
        try { ev.target.setPointerCapture(ev.pointerId); } catch (e) {}
      }
    },
    adGripMove: function (ev) {
      if (!adDrag) return;
      ev.preventDefault();
      adDrag.ghost.style.left = (ev.clientX + 10) + "px";
      adDrag.ghost.style.top = (ev.clientY - 14) + "px";
      // Auto-scroll when dragging near the viewport edges (long lists).
      if (ev.clientY < 90) window.scrollBy(0, -14);
      else if (ev.clientY > window.innerHeight - 70) window.scrollBy(0, 14);
      adClearOver();
      var t = adDropTarget(ev.clientX, ev.clientY);
      if (t && t.getAttribute("data-key") !== adDrag.key) t.classList.add("ad-over");
    },
    adGripUp: function (ev, pos) {
      if (!adDrag) return;
      var t = adDropTarget(ev.clientX, ev.clientY);
      var key = adDrag.key;
      FF.adGripCancel();
      adSel = null;
      // The browser may fire a click right after pointerup; don't let it
      // select/move a second time on the re-rendered list.
      adSwallowClick = true;
      setTimeout(function () { adSwallowClick = false; }, 0);
      if (!t) return;
      if (t.getAttribute("data-key")) {
        if (t.getAttribute("data-key") !== key) adMoveAbove(pos, key, t.getAttribute("data-key"));
      } else if (t.getAttribute("data-tier")) {
        adMoveTierEnd(pos, key, parseInt(t.getAttribute("data-tier"), 10));
      } else if (t.getAttribute("data-newtier")) {
        adNewBottomTier(pos, key);
      }
    },
    adGripCancel: function () {
      if (!adDrag) return;
      adClearOver();
      if (adDrag.ghost.parentNode) adDrag.ghost.parentNode.removeChild(adDrag.ghost);
      adDrag = null;
    },
    adClickRow: function (pos, key) {
      if (adSwallowClick) return;
      if (adSel && adSel !== key) { var k = adSel; adSel = null; adMoveAbove(pos, k, key); }
      else { adSel = adSel === key ? null : key; adminPage(pos); }
    },
    adClickTier: function (pos, tier) {
      if (adSwallowClick) return;
      if (adSel) { var k = adSel; adSel = null; adMoveTierEnd(pos, k, tier); }
    },
    adClickNew: function (pos) {
      if (adSwallowClick) return;
      if (adSel) { var k = adSel; adSel = null; adNewBottomTier(pos, k); }
    },
    adSplit: function (pos, key) { adSel = null; adSplitAt(pos, key); },
    adMergeUp: function (pos, tier) { adSel = null; adMergeUp(pos, tier); },
    adDeselect: function () { adSel = null; route(); },
    adReset: function () {
      if (!confirm("Discard all local tier edits?")) return;
      localStorage.removeItem(AD_STORE);
      AD = null; adSel = null;
      route();
    },
    adOverwrite: function (pos) {
      adInit();
      var dirty = adDirtyPositions();
      if (!dirty.length) { alert("No changes to push - move someone first."); return; }
      if (!WORKER_URL) { alert("No Worker URL configured."); return; }
      var code = localStorage.getItem("ff_admin_code") ||
                 prompt("Admin code (commissioner only):", "");
      if (!code) return;
      code = code.trim();
      // Position takeover: the whole edited position ships (order + literal
      // tier per player), so the master's tiers become exactly this board.
      var rows = [];
      dirty.forEach(function (p) {
        adPool(p).forEach(function (e) {
          rows.push(e.key + "," + Math.round(AD.ratings[e.key] * 100) / 100 +
                    "," + AD.tierOf[e.key]);
        });
      });
      var csv = "key,rating,tier\n" + rows.join("\n") + "\n";
      if (!confirm("Overwrite master tiers for " + dirty.join(", ") + " (" +
                   rows.length + " players)? Your board becomes the master for " +
                   "those positions and stays PINNED over the crowd blend until " +
                   "you release them.")) return;
      adSend(code, csv, function (j) {
        AD.changed = {};
        adSave();
        alert("Master overwrite saved (" + j.rows + " players). Rebuild: " +
              j.rebuild + ".\nThe live tiers refresh when the rebuild finishes (~2 min).");
        adminPage(pos);
      });
    },
    adRelease: function (pos) {
      adInit();
      if (!WORKER_URL) { alert("No Worker URL configured."); return; }
      var code = localStorage.getItem("ff_admin_code") ||
                 prompt("Admin code (commissioner only):", "");
      if (!code) return;
      code = code.trim();
      if (!confirm("Release " + pos + " back to the crowd? Its pinned tiers are " +
                   "dropped and the next rebuild derives them from ratings again.")) return;
      // Empty rating+tier rows = "unpin these players" to the rebuild.
      var csv = "key,rating,tier\n" + (DATA[pos] || []).map(function (e) {
        return e.key + ",,";
      }).join("\n") + "\n";
      adSend(code, csv, function (j) {
        Object.keys(AD.changed).forEach(function (k) {
          var e = BYKEY[k];
          if (e && e.pos === pos) delete AD.changed[k];
        });
        adSave();
        alert("Released " + pos + " to the crowd. Rebuild: " + j.rebuild + ".");
        adminPage(pos);
      });
    },
    setNote: function (pos, tier, text) {
      S.notes[pos] = S.notes[pos] || {};
      text = String(text || "").slice(0, 200);
      if (text) S.notes[pos][tier] = text; else delete S.notes[pos][tier];
      save(S);
    },
    choose: function (winner, loser, pos, el) {
      if (duelLock) return;              // ignore taps during the feedback beat
      duelLock = true;
      if (el) el.classList.add("chosen");
      pick(winner, loser);               // record immediately; only the render waits
      setTimeout(function () { duelLock = false; play(pos); }, PICK_MS);
    },
    again: function (pos) { if (!duelLock) play(pos); },
    noPick: function (a, b, pos) {
      // Neither interests you: drop both presented options (-10 Elo each),
      // count as a comparison for both, then show a new pair.
      if (duelLock) return;
      duelLock = true;
      buzz(BUZZ.fade);
      var cards = document.querySelectorAll(".card"), i;
      for (i = 0; i < cards.length; i++) cards[i].classList.add("dropped");
      S.ratings[a] -= NUDGE; S.ratings[b] -= NUDGE;
      S.comps[a]++; S.comps[b]++;
      S.fades[a] = (S.fades[a] || 0) + 1;   // 5 fades = benched from matchups
      S.fades[b] = (S.fades[b] || 0) + 1;   // until the next master rebuild
      save(S);
      setTimeout(function () { duelLock = false; play(pos); }, PICK_MS);
    },
    commitPicks: function () {
      // Push just the players you moved (rating != seed) to the repo through the
      // serverless proxy, which commits picks/u-<id>.csv on your behalf - no
      // popup, no download, no token in the browser. Each browser has a stable
      // id, so re-submitting overwrites your one file (one vote each). Run
      // "Rebuild Master Tiers" afterward to fold every user's file in.
      // Base stamp: which master these picks were played against. The rebuild
      // only blends files stamped with the master it is rebuilding, so picks
      // made on an older tier base never shift the new one.
      var base = String((window.FF_DATA || {}).base || "")
        .replace(/[^0-9a-f]/gi, "").slice(0, 16).toLowerCase();
      var rows = [];
      ALL.forEach(function (e) {
        if (Math.abs((S.ratings[e.key] || 0) - e.seed) > 0.001) {
          rows.push(e.key + "," + (Math.round(S.ratings[e.key] * 100) / 100) +
                    "," + (S.comps[e.key] || 0) + (base ? "," + base : ""));
        }
      });
      if (!rows.length) { alert("No picks to save yet - play a few matchups first."); return; }
      if (!WORKER_URL) {
        alert("Commit isn't wired up yet: no Worker URL is set.\n" +
              "See infra/commit-worker/README.md to deploy the proxy and set it.");
        return;
      }
      var csv = "key,rating,comps" + (base ? ",base" : "") + "\n" + rows.join("\n") + "\n";
      var id = userId();
      var btn = document.querySelector(".btn-primary");
      if (btn) { btn.disabled = true; btn.textContent = "Saving…"; }
      var done = function () { if (btn) { btn.disabled = false; btn.innerHTML = "&#128640; Commit to GitHub"; } };
      var attempt = function (code, canPromptOn401) {
        return fetch(WORKER_URL, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ id: id, csv: csv, code: code, ts: tsToken })
        }).then(function (r) {
          return r.json().catch(function () { return {}; }).then(function (j) {
            if (!r.ok) throw new Error(j.error || ("HTTP " + r.status));
            return j;
          });
        }).then(function () {
          if (code) localStorage.setItem(CODE_STORE, code);
          if (tsWidget !== null) { tsToken = ""; window.turnstile.reset(tsWidget); }
          done();
          buzz(BUZZ.saved);
          alert("Saved your rankings (id " + id + ", " + rows.length +
                " players). They'll be blended into the next community tiers.");
          flashBtn("flash-ok");         // alert() blocks paint, so flash after dismiss
        }).catch(function (err) {
          // Private-league mode: the server asks for a code - prompt once.
          if (canPromptOn401 && String(err.message).indexOf("league code") >= 0) {
            localStorage.removeItem(CODE_STORE);
            var typed = prompt("This league requires a code (ask the commissioner):", "");
            if (typed) return attempt(typed.trim(), false);
            done();
            return;
          }
          done();
          buzz(BUZZ.error);
          alert("Couldn't save: " + err.message);
          flashBtn("flash-err");
        });
      };
      attempt(localStorage.getItem(CODE_STORE) || "", true);
    },
    exportTiers: function () {
      // key + manual_tier first (so the CLI/Action can read it), then human
      // columns (name/pos/rookie) so you can edit it by hand in a spreadsheet.
      // tier_note repeats your tier description on each row of the tier, the
      // same layout the master CSV uses, so notes survive the round trip.
      var q = function (s) { return '"' + String(s == null ? "" : s).replace(/"/g, '""') + '"'; };
      var HEADERS = (window.FF_DATA && window.FF_DATA.stat_headers) || [];
      var lines = [["key,manual_tier,rating,comps,tier_note,name,pos,team,rookie,total,ppg"].concat(HEADERS).join(",")];
      ORDER.forEach(function (p) {
        var t = tiersFor(p);
        var pool = (DATA[p] || []).slice().sort(function (a, b) {
          return (t[a.key] - t[b.key]) || (S.ratings[b.key] - S.ratings[a.key]);
        });
        pool.forEach(function (e) {
          var c = e.cols || {};
          // rating is the continuous user rating the rebuild action averages.
          var row = [e.key, t[e.key], Math.round(S.ratings[e.key] * 100) / 100,
                     S.comps[e.key] || 0,
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
        var iComps = hasHeader ? cols.indexOf("comps") : -1;
        var n = 0;
        for (var i = hasHeader ? 1 : 0; i < lines.length; i++) {
          var parts = splitCsv(lines[i]);
          var key = (parts[iKey] || "").trim();
          if (!key || !(key in S.ratings)) continue;
          var rating = iRating >= 0 ? parseFloat(parts[iRating]) : NaN;
          var tier = parseInt(parts[iTier], 10);
          if (!isNaN(rating)) {
            S.ratings[key] = rating;            // continuous master rating: use as-is
            if (iComps >= 0) {                  // restore pick counts (device migration)
              var cN = parseInt(parts[iComps], 10);
              if (!isNaN(cN) && cN > 0) S.comps[key] = cN;
            }
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
    if (h.indexOf("#/levels") === 0) return levelsPage();
    m = h.match(/^#\/admin(?:\/(\w+))?/); if (m) return adminPage(m[1]);
    home();
  }
  window.addEventListener("hashchange", route);
  if (!ALL.length) { app.innerHTML = "<p>No player data found. Run <code>build-webapp</code> to generate data.js.</p>"; }
  else route();
})();
