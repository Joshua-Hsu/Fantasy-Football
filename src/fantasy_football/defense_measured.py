"""Measured defensive scheme: formations, blitz rate, box counts, key defenders.

The retired Gemini scouting loop wrote package percentages it could not
know. This module computes every number in a defense note from nflverse
release files instead - no model anywhere:

- **Formation** per play from the participation file (``defense_personnel``
  -> ``4-2-5 nickel``). It is the only source of true formation splits and
  publishes late; when the season's file is missing (2026 returns 404 as of
  2026-10-26) every formation field reads ``n/a`` and the block is labeled
  ``src: "ftn-only"``.
- **Rushers / box** per play from FTN charting (``n_pass_rushers``,
  ``n_defense_box``), joined to play-by-play on game + play id for the
  defense (``defteam``) and pass/run. Blitz = 5+ rushers on a pass play
  (sacks and scrambles included, pbp ``pass == 1``).
- **Key defenders** from snap counts (>= 50% of defensive snaps), with
  contract (``apy`` >= $15M) and draft pedigree (recent first-rounder, or a
  top-10 pick any year) as the tie-breaker for who is a star.

Two surfaces read it: ``season_blocks`` (the season-to-date block with a
``last3`` window, ``FF_DVP.scheme[TEAM].m``) and ``weekly_note`` (one
``WkN measured:`` row per defense per week in ``defense_notes.csv``).
``shift_flags`` marks a defense whose last two games moved away from its
earlier habit, so the hand-written profile can be revisited.

Inputs are plain dict rows so the tests run on fixtures; the ``fetch_*``
helpers are the only network code and callers treat their failures as
"that layer is missing", never as a failed build.
"""

from __future__ import annotations

import csv
import os
import re
from collections import Counter
from dataclasses import dataclass, field

PARTICIPATION_URL = ("https://github.com/nflverse/nflverse-data/releases/download/"
                     "pbp_participation/pbp_participation_{year}.csv")
FTN_URL = ("https://github.com/nflverse/nflverse-data/releases/download/"
           "ftn_charting/ftn_charting_{year}.csv")
CONTRACTS_URL = ("https://github.com/nflverse/nflverse-data/releases/download/"
                 "contracts/historical_contracts.parquet")

_REG_WEEKS = 18
#: Games in the "last 3" window of the season block.
WINDOW = 3
#: Key-defender rules (spec decision 6).
KEY_MIN_PCT = 0.50
KEY_COUNT = 6
STAR_APY = 15.0          # $M per year
STAR_BONUS = 0.25        # added to snap share; outweighs a 5-10 point snap gap
STAR_RECENT_YEARS = 4    # a first-rounder drafted within this many seasons
#: Shift rule (spec section D).
SHIFT_POINTS = 15
SHIFT_WEEKS = 2

_DL = {"DE", "DT", "NT", "DL"}
_LB = {"MLB", "ILB", "OLB", "LB"}
_DB = {"CB", "FS", "SS", "S", "DB"}
_GROUP = {**{p: "DL" for p in _DL}, **{p: "LB" for p in _LB}, **{p: "DB" for p in _DB}}

_NAMED = {
    (4, 3, 4): "base", (3, 4, 4): "base", (4, 4, 3): "base", (5, 2, 4): "base",
    (4, 2, 5): "nickel", (3, 3, 5): "nickel", (2, 4, 5): "nickel",
    (4, 1, 6): "dime", (3, 2, 6): "dime", (2, 3, 6): "dime",
}


# ---- formations ---------------------------------------------------------

def formation(personnel) -> tuple[str, int] | None:
    """``(label, n_db)`` for one ``defense_personnel`` string, or None.

    ``"3 CB, 2 DE, 2 DT, 1 FS, 1 MLB, 1 OLB, 1 SS"`` -> ``("4-2-5 nickel", 5)``.
    Named fronts per the spec table; 7+ DBs = quarter; 3 DBs with 6+ in the
    front = goal line; anything else keeps the raw ``D-L-B`` digits.
    Unparseable / empty strings return None (the play has no formation).
    """
    if personnel is None or (isinstance(personnel, float) and personnel != personnel):
        return None
    counts = {"DL": 0, "LB": 0, "DB": 0}
    other = 0
    seen = False
    for part in str(personnel).split(","):
        m = re.match(r"\s*(\d+)\s+([A-Z]+)\s*$", part)
        if not m:
            continue
        seen = True
        n, pos = int(m.group(1)), m.group(2)
        grp = _GROUP.get(pos)
        if grp:
            counts[grp] += n
        else:
            other += n
    if not seen:
        return None
    dl, lb, db = counts["DL"], counts["LB"], counts["DB"]
    digits = f"{dl}-{lb}-{db}"
    if dl + lb + db + other == 11 and not other:
        name = _NAMED.get((dl, lb, db))
        if name is None and db >= 7:
            name = "quarter"
        elif name is None and db <= 3 and dl + lb >= 6:
            name = "goal line"
        if name:
            return f"{digits} {name}", db
    return digits, db


# ---- per-play join ------------------------------------------------------

def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f


def scheme_plays(pbp, ftn=None, participation=None) -> list[dict]:
    """One dict per defensive scrimmage play: the joined, measured facts.

    ``pbp`` rows carry ``game_id``, ``play_id``, ``week``, ``defteam``,
    ``pass``, ``rush`` (regular season already filtered or not - weeks past
    18 are dropped here). ``ftn`` rows are keyed ``nflverse_game_id`` +
    ``nflverse_play_id``; ``participation`` rows ``nflverse_game_id`` +
    ``play_id``. Returns ``[{"tm", "wk", "gid", "pass", "rush", "label",
    "db", "rushers", "box"}]``; a field is None when its source has nothing
    for the play. A rusher/box count of 0 means "not charted" in both
    sources and is stored as None. Plays neither file covers yet are
    dropped, so a half-charted week never counts as a game played.
    """
    ftn_by = {}
    for r in ftn or []:
        key = (str(r.get("nflverse_game_id")), int(_num(r.get("nflverse_play_id")) or -1))
        ftn_by[key] = r
    part_by = {}
    for r in participation or []:
        key = (str(r.get("nflverse_game_id")), int(_num(r.get("play_id")) or -1))
        part_by[key] = r
    out = []
    for r in pbp:
        d = r.get("defteam")
        if not d or (isinstance(d, float) and d != d):
            continue
        wk = int(_num(r.get("week")) or 0)
        if not 1 <= wk <= _REG_WEEKS:
            continue
        if r.get("season_type") not in (None, "REG"):
            continue
        is_pass, is_rush = bool(_num(r.get("pass"))), bool(_num(r.get("rush")))
        if not (is_pass or is_rush):
            continue
        key = (str(r.get("game_id")), int(_num(r.get("play_id")) or -1))
        f, p = ftn_by.get(key), part_by.get(key)
        if f is None and p is None:
            continue  # not charted yet (FTN lags a day or two) - nothing measured
        label = db = None
        rushers = box = None
        if p is not None:
            fm = formation(p.get("defense_personnel"))
            if fm:
                label, db = fm
        if f is not None:
            rushers, box = _num(f.get("n_pass_rushers")), _num(f.get("n_defense_box"))
        elif p is not None:
            # No FTN row: participation carries the same two counts (NGS).
            rushers, box = _num(p.get("number_of_pass_rushers")), _num(p.get("defenders_in_box"))
        gd = r.get("game_date")
        out.append({"tm": str(d), "wk": wk, "gid": key[0], "pass": is_pass, "rush": is_rush,
                    "date": str(gd)[:10] if gd is not None and gd == gd else "",
                    "label": label, "db": db,
                    "rushers": rushers if rushers else None, "box": box if box else None})
    return out


# ---- aggregation --------------------------------------------------------

def _pct(n, d):
    return round(100.0 * n / d) if d else None


def aggregate(plays) -> dict:
    """Measured fields over a list of :func:`scheme_plays` rows.

    ``{"base": [label, pct] | None, "second": [label, pct] | None,
    "db5": pct | None, "blitz": pct | None, "rushers": 4.3 | None,
    "box7": pct | None, "light": pct | None, "src": "participation" |
    "ftn-only", "n": plays}``. Percentages are whole numbers; ``rushers``
    keeps one decimal (an average count, not a share).
    """
    labeled = [p for p in plays if p["label"]]
    out: dict = {"n": len(plays)}
    if labeled:
        c = Counter(p["label"] for p in labeled)
        top = c.most_common(2)
        out["base"] = [top[0][0], _pct(top[0][1], len(labeled))]
        out["second"] = [top[1][0], _pct(top[1][1], len(labeled))] if len(top) > 1 else None
        out["db5"] = _pct(sum(1 for p in labeled if (p["db"] or 0) >= 5), len(labeled))
        out["src"] = "participation"
    else:
        out["base"] = out["second"] = out["db5"] = None
        out["src"] = "ftn-only"
    rush = [p["rushers"] for p in plays if p["pass"] and p["rushers"]]
    out["blitz"] = _pct(sum(1 for r in rush if r >= 5), len(rush))
    out["rushers"] = round(sum(rush) / len(rush), 1) if rush else None
    boxes = [p["box"] for p in plays if p["box"]]
    out["box7"] = _pct(sum(1 for b in boxes if b >= 7), len(boxes))
    out["light"] = _pct(sum(1 for b in boxes if b <= 6), len(boxes))
    return out


def _by_team_week(plays) -> dict[str, dict[int, list[dict]]]:
    out: dict[str, dict[int, list[dict]]] = {}
    for p in plays:
        out.setdefault(p["tm"], {}).setdefault(p["wk"], []).append(p)
    return out


def weekly_blocks(plays) -> dict[str, dict[int, dict]]:
    """``{team: {week: aggregate}}`` - one block per game played."""
    return {t: {w: aggregate(ps) for w, ps in sorted(wks.items())}
            for t, wks in _by_team_week(plays).items()}


# ---- key defenders ------------------------------------------------------

def norm_name(name) -> str:
    """Lowercase, suffixes (Jr/Sr/II-V) and punctuation stripped."""
    n = re.sub(r"\b(jr|sr|ii|iii|iv|v)\b\.?", "", str(name or "").lower())
    return re.sub(r"[^a-z]", "", n)


def contract_index(rows) -> dict:
    """Active contracts de-duplicated per player (highest ``apy`` kept).

    Returns ``{"exact": {name: rec}, "norm": {norm_name: rec}, "gsis":
    {gsis_id: rec}}`` where rec is ``{"apy", "draft_year", "draft_round",
    "draft_overall"}``. The gsis index is what catches nicknames: snap
    counts say "Riq Woolen", the contract says "Tariq Woolen".
    """
    best: dict[str, dict] = {}
    gsis: dict[str, dict] = {}
    for r in rows or []:
        if not r.get("is_active"):
            continue
        name = str(r.get("player") or "").strip()
        if not name:
            continue
        apy = _num(r.get("apy")) or 0.0
        rec = {"apy": apy, "draft_year": _num(r.get("draft_year")),
               "draft_round": _num(r.get("draft_round")),
               "draft_overall": _num(r.get("draft_overall"))}
        gid = r.get("gsis_id")
        if isinstance(gid, str) and gid and (gid not in gsis or gsis[gid]["apy"] < apy):
            gsis[gid] = rec
        if name in best and best[name]["apy"] >= apy:
            continue
        best[name] = rec
    norm: dict[str, dict] = {}
    for name, rec in best.items():
        k = norm_name(name)
        if k not in norm or norm[k]["apy"] < rec["apy"]:
            norm[k] = rec
    return {"exact": best, "norm": norm, "gsis": gsis}


def star_tags(rec: dict | None, season: int) -> str:
    """``"$"``, ``"R1"``, ``"$ R1"`` or ``""`` for one contract record."""
    if not rec:
        return ""
    tags = []
    if (rec.get("apy") or 0) >= STAR_APY:
        tags.append("$")
    rnd, yr, ovr = rec.get("draft_round"), rec.get("draft_year"), rec.get("draft_overall")
    if (rnd == 1 and yr is not None and yr >= season - STAR_RECENT_YEARS) or \
            (ovr is not None and ovr <= 10):
        tags.append("R1")
    return " ".join(tags)


def _group(pos: str) -> str | None:
    return _GROUP.get(str(pos or "").upper())


def key_defenders(snap_rows, *, through_week: int, season: int, contracts=None,
                  official=None, unmatched: set | None = None,
                  ids: dict[str, str] | None = None) -> dict[str, list[dict]]:
    """Top defenders per team: snap share first, star pedigree as tie-break.

    Candidates average >= 50% ``defense_pct`` over the games they played
    (any snap) and played >= 2 games, or every team game when the team has
    played only one. Score = snap share + :data:`STAR_BONUS` for a star
    (``$`` or ``R1``). The top six are taken with at least one DL, LB and
    DB when the team has one. ``out`` is True when the official report
    lists the player Out or Doubtful. Contract names are matched exactly,
    then normalized; misses go into ``unmatched`` (never guessed). With
    ``ids`` (``{pfr_id: gsis_id}`` from the roster file) the snap row's
    ``pfr_player_id`` is tried first - an exact id join, not a guess.
    Returns ``{team: [{"n", "pos", "snap", "star", "out"}]}``.
    """
    idx = contracts if isinstance(contracts, dict) and "exact" in contracts \
        else contract_index(contracts)
    games: dict[str, set[int]] = {}
    hist: dict[tuple[str, str], dict] = {}
    for r in snap_rows or []:
        wk = int(_num(r.get("week")) or 0)
        if not 1 <= wk <= through_week:
            continue
        pos = str(r.get("position") or "").upper()
        if not _group(pos):
            continue
        team, name = str(r.get("team")), str(r.get("player"))
        pct = _num(r.get("defense_pct")) or 0.0
        games.setdefault(team, set()).add(wk)
        if pct <= 0:
            continue
        ent = hist.setdefault((team, name), {"pos": pos, "w": {},
                                             "pfr": r.get("pfr_player_id")})
        ent["w"][wk] = max(ent["w"].get(wk, 0.0), pct)
    out_names = {(r.get("tm"), norm_name(r.get("n"))) for r in official or []
                 if r.get("st") in ("OUT", "DOUBTFUL")}
    cands: dict[str, list[dict]] = {}
    for (team, name), ent in hist.items():
        played = len(ent["w"])
        need = 1 if len(games.get(team, ())) <= 1 else 2
        avg = sum(ent["w"].values()) / played
        if played < need or avg < KEY_MIN_PCT:
            continue
        gid = (ids or {}).get(ent["pfr"]) if isinstance(ent["pfr"], str) else None
        rec = (idx.get("gsis", {}).get(gid) if gid else None) \
            or idx["exact"].get(name) or idx["norm"].get(norm_name(name))
        if rec is None and unmatched is not None:
            unmatched.add(f"{team} {name}")
        star = star_tags(rec, season)
        cands.setdefault(team, []).append({
            "n": name, "pos": ent["pos"], "snap": round(100 * avg), "star": star,
            "out": (team, norm_name(name)) in out_names,
            "_s": avg + (STAR_BONUS if star else 0.0), "_g": _group(ent["pos"])})
    result: dict[str, list[dict]] = {}
    for team, cs in cands.items():
        cs.sort(key=lambda c: (-c["_s"], -c["snap"], c["n"]))
        pick = []
        for grp in ("DL", "LB", "DB"):
            best = next((c for c in cs if c["_g"] == grp), None)
            if best:
                pick.append(best)
        for c in cs:
            if len(pick) >= KEY_COUNT:
                break
            if c not in pick:
                pick.append(c)
        pick.sort(key=lambda c: (-c["_s"], -c["snap"], c["n"]))
        result[team] = [{k: v for k, v in c.items() if not k.startswith("_")}
                        for c in pick[:KEY_COUNT]]
    return result


# ---- season block + shift ------------------------------------------------

def season_blocks(plays, *, key: dict[str, list[dict]] | None = None,
                  window: int = WINDOW) -> dict[str, dict]:
    """Season-to-date block per defense with the last ``window`` games inside.

    ``{team: {...aggregate..., "last3": {...aggregate...}, "key": [...],
    "wk": last week counted}}`` (``FF_DVP.scheme[TEAM].m``).
    """
    out: dict[str, dict] = {}
    for team, wks in _by_team_week(plays).items():
        weeks = sorted(wks)
        allp = [p for w in weeks for p in wks[w]]
        blk = aggregate(allp)
        blk["last3"] = aggregate([p for w in weeks[-window:] for p in wks[w]])
        blk["wk"] = weeks[-1]
        blk["key"] = (key or {}).get(team, [])
        out[team] = blk
    return out


def shift_flags(plays, profiles: dict[str, dict] | None = None, *,
                weeks: int = SHIFT_WEEKS, points: int = SHIFT_POINTS) -> dict[str, dict]:
    """Defenses that have played away from their current profile for a while.

    The baseline is what the profile describes: the measured block over
    the games played BEFORE the profile row's date (``profiles[TEAM]["d"]``).
    Every game since is compared with it; a shift is a run of ``weeks`` or
    more consecutive games, ending with the latest, in which the base
    formation differs from the profile's, or the 5+ DB share or blitz rate
    sits ``points`` or more off it in the same direction (7+ box stands in
    for the formation fields while participation is missing). Appending a
    new ``defense_profiles.csv`` row moves the baseline forward, so the
    flag clears once the profile has been rewritten.

    A team with no dated profile falls back to its last ``weeks`` games vs
    the games before them. Needs two baseline games.
    Returns ``{team: {"since": week, "what": "blitz 24 -> 41; ...",
    "vs": profile date | None}}``.
    """
    out: dict[str, dict] = {}
    for team, wks in _by_team_week(plays).items():
        order = sorted(wks)
        pdate = ((profiles or {}).get(team) or {}).get("d") or ""
        dates = {w: min((p.get("date") or "") for p in wks[w]) for w in order}
        if pdate and all(dates.values()):
            earlier = [w for w in order if dates[w] < pdate]
            recent = [w for w in order if dates[w] >= pdate]
        else:
            pdate = ""
            earlier, recent = order[:-weeks], order[-weeks:]
        if len(earlier) < 2 or len(recent) < weeks:
            continue
        base = aggregate([p for w in earlier for p in wks[w]])
        rec = {w: aggregate(wks[w]) for w in recent}

        def streak(off) -> list[int]:
            """Consecutive recent weeks (ending with the latest) where off(w) holds."""
            run = []
            for w in reversed(recent):
                if not off(w):
                    break
                run.insert(0, w)
            return run if len(run) >= weeks else []

        what, since = [], []
        if base["base"]:
            run = streak(lambda w: rec[w]["base"] is not None
                         and rec[w]["base"][0] != base["base"][0])
            if run:
                now = aggregate([p for w in run for p in wks[w]])["base"][0]
                what.append(f"base {base['base'][0]} -> {now}")
                since.append(run[0])
        fields = (("db5", "5+ DB"), ("blitz", "blitz")) if base["src"] == "participation" \
            else (("box7", "7+ box"), ("blitz", "blitz"))
        for k, label in fields:
            if base[k] is None:
                continue
            for sign in (1, -1):
                run = streak(lambda w: rec[w][k] is not None
                             and sign * (rec[w][k] - base[k]) >= points)
                if run:
                    now = aggregate([p for w in run for p in wks[w]])[k]
                    what.append(f"{label} {base[k]} -> {now}")
                    since.append(run[0])
        if what:
            out[team] = {"since": min(since), "what": "; ".join(what), "vs": pdate or None}
    return out


# ---- weekly note --------------------------------------------------------

def _key_text(key) -> str:
    bits = []
    for k in key or []:
        s = f"{k['n']} {k['pos']} {k['snap']}%"
        if k.get("star"):
            s += " " + k["star"]
        if k.get("out"):
            s += " OUT"
        bits.append(s)
    return ", ".join(bits) if bits else "n/a"


def weekly_note(week: int, blk: dict, key) -> str:
    """The ``WkN measured:`` note text for one defense-week."""
    na = "n/a"
    blitz = f"{blk['blitz']}%" if blk.get("blitz") is not None else na
    rushers = f"{blk['rushers']}" if blk.get("rushers") is not None else na
    box7 = f"{blk['box7']}%" if blk.get("box7") is not None else na
    if blk.get("src") == "participation":
        base = f"{blk['base'][0]} {blk['base'][1]}%"
        second = f"{blk['second'][0]} {blk['second'][1]}%" if blk.get("second") else na
        head = f"base {base} · 2nd {second} · 5+ DB {blk['db5']}%"
        rush = f"blitz {blitz} (avg {rushers} rushers, 7+ box {box7})"
    else:
        light = f"{blk['light']}%" if blk.get("light") is not None else na
        head = (f"base/2nd: n/a (no participation file) · 7+ box {box7} · "
                f"light box (<=6) {light} · 5+ DB n/a")
        rush = f"blitz {blitz} (avg {rushers} rushers)"
    return f"Wk{week} measured: {head} · {rush} · key: {_key_text(key)}"[:900]


# ---- the game story (box scores + snaps + schedule, all computed) ------

#: Personnel thresholds for the story: a "core" defender, a real role change.
STORY_CORE = 0.60
STORY_ABSENT = 0.15
STORY_NEW = 0.40
STORY_ROLE_POINTS = 25
STORY_STEP_UP = 15
STORY_PRODUCERS = 4
_DVP_POS = ("QB", "RB", "WR", "TE")


def _scheme_text(blk: dict) -> str:
    """The measured scheme clause (formation only when participation exists)."""
    na = "n/a"
    blitz = f"{blk['blitz']}%" if blk.get("blitz") is not None else na
    rushers = f"{blk['rushers']}" if blk.get("rushers") is not None else na
    box7 = f"{blk['box7']}%" if blk.get("box7") is not None else na
    bits = []
    if blk.get("src") == "participation" and blk.get("base"):
        bits.append(f"base {blk['base'][0]} {blk['base'][1]}%")
        if blk.get("second"):
            bits.append(f"2nd {blk['second'][0]} {blk['second'][1]}%")
        bits.append(f"5+ DB {blk['db5']}%")
    bits.append(f"blitz {blitz} (avg {rushers} rushers)")
    bits.append(f"7+ box {box7}")
    if blk.get("src") != "participation":
        bits.append("formation n/a (no participation file)")
    return ", ".join(bits)


def _line(name: str, pos: str, st) -> str:
    """One compact box line: ``CeeDee Lamb WR 5-74-2 (12 tgt)``."""
    rush = (f"{st.rush_attempts}-{st.rush_yards}-{st.rush_touchdowns} rush"
            if st.rush_attempts else "")
    if pos == "QB":
        out = f"{name} QB {st.pass_yards} yds {st.pass_touchdowns} TD"
        if st.interceptions_thrown:
            out += f" {st.interceptions_thrown} INT"
        if st.rush_attempts and (st.rush_yards >= 15 or st.rush_touchdowns):
            out += f", {rush}"
        return out
    rec = (f"{st.receptions}-{st.receiving_yards}-{st.receiving_touchdowns}"
           f" ({st.targets} tgt)" if st.targets else "")
    if pos == "RB":
        parts = [p for p in (rush, rec and rec + " rec") if p]
    else:
        parts = [p for p in (rec, rush) if p]
    return f"{name} {pos} " + ", ".join(parts)


def personnel_changes(snap_rows, team: str, week: int) -> list[str]:
    """What changed in the defense's personnel this week, from snap counts.

    ``out``: a core defender (avg >= 60% before) absent this week - shown
    for the first two missed games only, after that it is the roster, not
    news. ``back``: a core defender playing 60%+ after missing the game
    before. ``new``: 40%+ this week with no real snaps earlier this season.
    A role change: a 50%+ regular whose share moved 25+ points, or a
    rotation player who jumped 15+ points to 40%+ (who absorbed the work).
    At most six items, absences first.
    """
    weeks: set[int] = set()
    hist: dict[str, dict] = {}
    for r in snap_rows or []:
        if str(r.get("team")) != team or not _group(str(r.get("position") or "")):
            continue
        wk = int(_num(r.get("week")) or 0)
        if not 1 <= wk <= week:
            continue
        weeks.add(wk)
        pct = _num(r.get("defense_pct")) or 0.0
        h = hist.setdefault(str(r.get("player")), {})
        h[wk] = max(h.get(wk, 0.0), pct)
    if week not in weeks:
        return []
    order = sorted(weeks)
    prev_weeks = order[:-1]
    out, back, new, role = [], [], [], []
    for name, h in hist.items():
        before = [h[w] for w in prev_weeks if h.get(w, 0) > STORY_ABSENT]
        avg = sum(before) / len(before) if before else 0.0
        now = h.get(week, 0.0)
        short = name
        if before and avg >= STORY_CORE and now <= STORY_ABSENT:
            missed = 0
            for w in reversed(order):
                if h.get(w, 0) > STORY_ABSENT:
                    break
                missed += 1
            if missed <= 2:
                nth = "2nd game" if missed == 2 else "1st game"
                out.append((-avg, f"{short} out ({nth})"))
        elif before and avg >= STORY_CORE and now >= STORY_CORE and prev_weeks \
                and h.get(prev_weeks[-1], 0) <= STORY_ABSENT:
            back.append((-now, f"{short} back {round(100 * now)}%"))
        elif not before and now >= STORY_NEW:
            new.append((-now, f"{short} {round(100 * now)}%"))
        elif before and avg >= 0.5 and now > STORY_ABSENT \
                and abs(now - avg) * 100 >= STORY_ROLE_POINTS:
            role.append((-abs(now - avg), f"{short} {round(100 * avg)}→{round(100 * now)}%"))
        elif before and avg < 0.5 and now >= STORY_NEW \
                and (now - avg) * 100 >= STORY_STEP_UP:
            # A rotation player taking a bigger share: who absorbed the work.
            role.append((-(now - avg), f"{short} {round(100 * avg)}→{round(100 * now)}%"))
    bits = [t for _, t in sorted(out)] + [t for _, t in sorted(back)]
    if new:
        bits.append("first snaps this season: " + ", ".join(t for _, t in sorted(new)))
    bits += [t for _, t in sorted(role)]
    return bits[:6]


def story_note(session, year: int, week: int, team: str, blk: dict, *,
               snap_rows=None, official=None, rules=None) -> str | None:
    """The full ``WkN measured:`` story for one defense-week, or None.

    Every piece is computed: the opponent and score (schedule), who
    produced against the defense (box scores, by fantasy points), what
    changed in its personnel (snap counts), the measured scheme (FTN /
    participation), points allowed by position against its own season
    average (the DvP table), and the next opponent with the season ranks
    a lineup call quotes. ``None`` when the DB does not have the game, so
    the caller falls back to :func:`weekly_note`.
    """
    from sqlalchemy import select

    from .matchups import defense_profiles
    from .models import Game, Player, PlayerGameStats, Team
    from .scoring import DEFAULT_RULES, score_stats

    rules = rules or DEFAULT_RULES
    abbr = {t.id: t.abbreviation for t in session.execute(select(Team)).scalars()}
    tid = next((i for i, a in abbr.items() if a == team), None)
    if tid is None:
        return None
    games = session.execute(
        select(Game).where(Game.season_year == year, Game.week >= week,
                           Game.week <= _REG_WEEKS,
                           (Game.home_team_id == tid) | (Game.away_team_id == tid))
        .order_by(Game.week)).scalars().all()
    game = next((g for g in games if g.week == week), None)
    if game is None or game.home_score is None:
        return None
    home = game.home_team_id == tid
    opp_id = game.away_team_id if home else game.home_team_id
    opp = abbr.get(opp_id, "?")
    pf, pa = (game.home_score, game.away_score) if home else (game.away_score, game.home_score)
    res = "W" if pf > pa else ("L" if pf < pa else "T")
    head = f"Wk{week} measured: {'v' if home else '@'}{opp}, {res} {pf}-{pa}."

    scored = []
    for st, pl in session.execute(
            select(PlayerGameStats, Player).join(Player, Player.id == PlayerGameStats.player_id)
            .where(PlayerGameStats.game_id == game.id,
                   PlayerGameStats.team_id == opp_id)).all():
        pos = (st.position or pl.position or "").upper()
        if pos not in _DVP_POS:
            continue
        pts = score_stats(st, rules)
        if pts > 0:
            scored.append((-pts, _line(pl.full_name, pos, st)))
    scored.sort()
    lines = [t for _, t in scored[:STORY_PRODUCERS]]

    pers = personnel_changes(snap_rows, team, week)
    prof = defense_profiles(session, year, rules=rules, through_week=week).get(team)
    read = []
    if prof and week in prof["wk"]:
        i = prof["wk"].index(week)
        rows = []
        for pos in _DVP_POS:
            vals = [v for v in prof["pa"][pos] if v is not None]
            if not vals:
                continue
            avg = sum(vals) / len(vals)
            rows.append((-(prof["pa"][pos][i] - avg),
                         f"{pos} {round(prof['pa'][pos][i])} (avg {round(avg)})"))
        read = [t for _, t in sorted(rows)]

    nxt = next((g for g in games if g.week > week), None)
    tail = ""
    if nxt is not None and prof:
        nhome = nxt.home_team_id == tid
        nopp = abbr.get(nxt.away_team_id if nhome else nxt.home_team_id, "?")
        ranks = ", ".join(f"{p} {prof['rk'][p]}" for p in _DVP_POS if p in prof["rk"])
        tail = (f" Wk{nxt.week} {'v' if nhome else '@'}{nopp}: season rank vs {ranks}"
                f" (32 = softest).")
        outs = [r["n"] for r in official or [] if r.get("tm") == team
                and r.get("st") in ("OUT", "DOUBTFUL") and _group(r.get("pos", ""))]
        if outs:
            tail = tail[:-1] + f"; report OUT/D: {', '.join(outs[:3])}."

    def build(n_lines: int) -> str:
        parts = [head]
        if lines:
            parts.append("Faced: " + "; ".join(lines[:n_lines]) + ".")
        if pers:
            parts.append("Personnel: " + "; ".join(pers) + ".")
        parts.append("Scheme: " + _scheme_text(blk) + ".")
        if read:
            parts.append("Fantasy read: allowed " + ", ".join(read) + ".")
        return " ".join(parts) + tail

    for n in range(len(lines), -1, -1):
        text = build(n)
        if len(text) <= 900:
            return text
    return text[:900]


def measured_rows(path: str) -> set[tuple[str, int]]:
    """``{(team, week)}`` already carrying a ``WkN measured:`` note."""
    if not path or not os.path.exists(path):
        return set()
    out = set()
    with open(path, newline="") as fh:
        for r in csv.DictReader(fh):
            m = re.match(r"\s*Wk(\d+) measured:", r.get("note") or "")
            if m:
                out.add(((r.get("team") or "").strip().upper(), int(m.group(1))))
    return out


def write_measured_notes(path: str, notes: dict[str, str], *, week: int, date: str,
                         force: bool = False) -> tuple[int, list[str]]:
    """Append ``WkN measured:`` rows; skip teams that have one unless ``force``.

    With ``force`` the team's existing row for that week is replaced (the
    file is rewritten without it) so a refresh never doubles a week.
    Other rows - hand pastes, the archived auto notes - are untouched.
    Returns ``(written, skipped_teams)``.
    """
    have = measured_rows(path)
    skipped = [t for t in notes if (t, week) in have and not force]
    todo = {t: n for t, n in notes.items() if t not in skipped and n}
    if force and os.path.exists(path):
        with open(path, newline="") as fh:
            rows = list(csv.reader(fh))
        pat = re.compile(rf"\s*Wk{week} measured:")
        keep = [r for r in rows[1:] if not (len(r) >= 3 and r[0].strip().upper() in todo
                                            and pat.match(r[2]))]
        if len(keep) != len(rows) - 1:
            with open(path, "w", newline="") as fh:
                w = csv.writer(fh)
                w.writerow(rows[0])
                w.writerows(keep)
    new = not os.path.exists(path)
    with open(path, "a", newline="") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(["team", "date", "note"])
        for team in sorted(todo):
            w.writerow([team, date, todo[team]])
    return len(todo), skipped


# ---- sources ------------------------------------------------------------

@dataclass
class SchemeSources:
    """Every release file the measured layer reads, fetched once per run."""

    year: int
    pbp: list = field(default_factory=list)
    ftn: list = field(default_factory=list)
    participation: list = field(default_factory=list)
    snaps: list = field(default_factory=list)
    contracts: list = field(default_factory=list)
    official: list = field(default_factory=list)
    ids: dict = field(default_factory=dict)       # pfr_id -> gsis_id (rosters)
    errors: dict = field(default_factory=dict)

    def plays(self) -> list[dict]:
        return scheme_plays(self.pbp, self.ftn, self.participation)


def fetch_participation(year: int) -> list[dict]:
    """Participation rows (404 until nflverse publishes the season)."""
    import pandas as pd

    cols = ["nflverse_game_id", "play_id", "defense_personnel",
            "defenders_in_box", "number_of_pass_rushers"]
    return pd.read_csv(PARTICIPATION_URL.format(year=year), usecols=cols).to_dict("records")


def fetch_ftn(year: int) -> list[dict]:
    import pandas as pd

    cols = ["nflverse_game_id", "nflverse_play_id", "week", "n_defense_box",
            "n_pass_rushers", "n_blitzers"]
    return pd.read_csv(FTN_URL.format(year=year), usecols=cols).to_dict("records")


def fetch_contracts() -> list[dict]:
    import pandas as pd

    cols = ["player", "gsis_id", "position", "is_active", "apy", "draft_year",
            "draft_round", "draft_overall"]
    df = pd.read_parquet(CONTRACTS_URL, columns=cols)
    return df[df["is_active"] == True].to_dict("records")  # noqa: E712 - pandas mask


def fetch_roster_ids(year: int) -> dict[str, str]:
    """``{pfr_id: gsis_id}`` from the season roster (links snaps to contracts)."""
    import pandas as pd

    from .ingest.nflverse import ROSTER_URL

    df = pd.read_parquet(ROSTER_URL.format(year=year), columns=["gsis_id", "pfr_id"])
    df = df.dropna()
    return dict(zip(df["pfr_id"].astype(str), df["gsis_id"].astype(str)))


def load_sources(year: int, *, pbp=None, snaps=None, official=None) -> SchemeSources:
    """Fetch what is missing; record (not raise) any layer that fails."""
    from .matchups import fetch_scheme_pbp, fetch_snap_counts

    src = SchemeSources(year=year, pbp=pbp or [], snaps=snaps or [], official=official or [])
    jobs = [("pbp", lambda: fetch_scheme_pbp(year), not src.pbp),
            ("snaps", lambda: fetch_snap_counts(year), not src.snaps),
            ("ftn", lambda: fetch_ftn(year), True),
            ("participation", lambda: fetch_participation(year), True),
            ("contracts", fetch_contracts, True),
            ("ids", lambda: fetch_roster_ids(year), True)]
    for name, fn, need in jobs:
        if not need:
            continue
        try:
            setattr(src, name, fn())
        except Exception as exc:  # noqa: BLE001 - a missing layer degrades, never fails
            src.errors[name] = str(exc)[:200]
    return src


def scheme_payload(src: SchemeSources, profiles: dict[str, dict] | None = None,
                   *, unmatched: set | None = None) -> dict[str, dict]:
    """``FF_DVP.scheme``: profile text + season measured block + shift flag."""
    plays = src.plays()
    through = max((p["wk"] for p in plays), default=0)
    key = key_defenders(src.snaps, through_week=through or _REG_WEEKS, season=src.year,
                        contracts=src.contracts, official=src.official,
                        unmatched=unmatched, ids=src.ids) if src.snaps else {}
    blocks = season_blocks(plays, key=key)
    shifts = shift_flags(plays, profiles)
    out: dict[str, dict] = {}
    for team in sorted(set(blocks) | set(profiles or {})):
        ent: dict = {}
        prof = (profiles or {}).get(team)
        if prof:
            ent["d"], ent["t"] = prof["d"], prof["t"]
        if team in blocks:
            ent["m"] = blocks[team]
        if team in shifts:
            ent["shift"] = shifts[team]
        out[team] = ent
    return out
