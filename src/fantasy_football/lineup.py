"""The weekly lineup answer as a command (``cli lineup``).

Lineup questions used to be answered from one chat session's memory; this
module makes the answer reproducible. It reads two things only:

- the committed ``docs/dvp.js`` sidecar (``window.FF_DVP``) - the coming
  week's opponents, defense profiles (season rank / last-3 rank / trend per
  position), Vegas implied totals, the official injury report and the
  personnel flags;
- the local DB - each rostered player's season PPG and his last three
  games (targets, carries, fantasy points under the default rules).

``roster.csv`` (``player,pos,team,slot``) is the only source of truth for
who is rostered; the slot column is free text and ignored.

The projection is a straight port of the app's matchup model (``modelProj``
in ``docs/app.js``)::

    proj = ppg x vegas^.45 x dvp^.35 x pace^.2

each factor a clamped ratio to league average; DvP blends the current
season with the baseline year by ``wNow = min(through_week / 6, .9)``; K
keys off its own implied total, DST inverts the opponent's. The one
deliberate difference is the ``ppg`` fed in: recent form is blended with
the season average (``RECENT_FORM_WEIGHT``) once a player has
``RECENT_FORM_MIN_GAMES`` games.

Everything below ``resolve_roster`` / ``usage_rows`` is pure (payload +
rows in, lineup out) so tests need neither network nor a populated DB.
"""

from __future__ import annotations

import csv
import json
import os
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

#: Weight of the last-3-games PPG in the model's ``ppg`` (season gets the
#: rest). Applied only once a player has ``RECENT_FORM_MIN_GAMES`` games;
#: before that the season average already IS recent form.
RECENT_FORM_WEIGHT = 0.5
RECENT_FORM_MIN_GAMES = 3

#: League lineup (CLAUDE.md): 1 QB / 2 RB / 2 WR / 1 TE / 1 FLEX / 1 K / 1 DEF.
SLOTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("QB", ("QB",)),
    ("RB1", ("RB",)),
    ("RB2", ("RB",)),
    ("WR1", ("WR",)),
    ("WR2", ("WR",)),
    ("TE", ("TE",)),
    ("FLEX", ("RB", "WR", "TE")),
    ("K", ("K",)),
    ("DST", ("DST",)),
)

#: Ranked alternatives shown under every slot.
ALTERNATIVES = 3

#: Sidecar fields the command needs; an older dvp.js without them is refused.
REQUIRED_FIELDS = ("year", "through_week", "teams", "opp", "profiles", "vegas", "official")

#: Official designations that bench a player outright vs ones that only flag.
EXCLUDE_STATUSES = ("OUT", "DOUBTFUL")
FLAG_STATUSES = ("QUESTIONABLE", "DNP")

_PREFIX = "window.FF_DVP = "


class LineupError(Exception):
    """A clean, user-facing failure (missing sidecar, unmatched roster names)."""


# --------------------------------------------------------------------------
# Inputs
# --------------------------------------------------------------------------

def norm_name(n: str) -> str:
    """Case/suffix/punctuation-insensitive name key (same rule as ``scout._norm_name``)."""
    n = re.sub(r"\s+(Jr\.?|Sr\.?|II|III|IV|V)$", "", (n or "").strip(), flags=re.I)
    return re.sub(r"\s+", " ", re.sub(r"[^a-z ]", "", n.lower())).strip()


def load_payload(path: str) -> dict:
    """Parse ``docs/dvp.js`` (the JSON after ``window.FF_DVP = ``) and check its fields."""
    if not path or not os.path.exists(path):
        raise LineupError(f"{path} not found - build it with "
                          "`python -m fantasy_football.cli dvp --out docs/dvp.js`")
    text = open(path, encoding="utf-8").read()
    i = text.find(_PREFIX)
    if i < 0:
        raise LineupError(f"{path} does not look like a dvp.js sidecar (no `{_PREFIX.strip()}`)")
    body = text[i + len(_PREFIX):].strip().rstrip(";").strip()
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise LineupError(f"{path}: could not parse the FF_DVP JSON ({exc})") from exc
    check_payload(payload, path)
    return payload


def check_payload(payload: dict, path: str = "dvp.js") -> None:
    missing = [f for f in REQUIRED_FIELDS if f not in payload]
    if missing:
        raise LineupError(f"{path} predates the fields `cli lineup` needs (missing: "
                          f"{', '.join(missing)}) - rebuild it with `cli dvp`")


def read_roster(path: str) -> list[dict]:
    """``roster.csv`` -> ``[{"player", "pos", "team"}]`` (slot is ignored)."""
    if not path or not os.path.exists(path):
        raise LineupError(f"{path} not found")
    out = []
    with open(path, newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            name = (r.get("player") or "").strip()
            if not name:
                continue
            pos = (r.get("pos") or "").strip().upper()
            pos = "DST" if pos in ("DEF", "D/ST", "DST") else pos
            out.append({"player": name, "pos": pos, "team": (r.get("team") or "").strip().upper()})
    return out


# --------------------------------------------------------------------------
# DB: roster resolution + usage
# --------------------------------------------------------------------------

def _played(query, Game, before_week: int | None):
    query = query.where(Game.season_type == "regular", Game.home_score.is_not(None))
    return query.where(Game.week < before_week) if before_week else query


def final_this_week(session: Session, year: int, week: int) -> set[str]:
    """Teams whose ``week`` game already has a final score (lineup slot locked)."""
    from .models import Game, Team

    abbr = {t.id: t.abbreviation for t in session.execute(select(Team)).scalars()}
    out: set[str] = set()
    for g in session.execute(select(Game).where(
            Game.season_year == year, Game.week == week, Game.season_type == "regular",
            Game.home_score.is_not(None))).scalars():
        out.update(x for x in (abbr.get(g.home_team_id), abbr.get(g.away_team_id)) if x)
    return out


def resolve_roster(session: Session, roster: list[dict], year: int,
                   week: int | None = None) -> tuple[list[dict], list[str]]:
    """Match roster names to DB players; return ``(rows, unmatched_names)``.

    Each row carries the season PPG, games played and the last three games
    for the model. DST rows use the team code in the player column. Names
    match case-insensitively with suffixes/punctuation stripped; ties go to
    the player at the roster's position, then team, then the one who has
    played this season. Nothing is guessed: no match = unmatched.

    ``week`` is the sidecar's coming week: usage counts only games BEFORE it
    (a Sunday-afternoon run must not feed the week's own early results into
    the projection), and a row whose team already finished that game is
    marked ``locked``.
    """
    from .models import Player, Team

    by_name: dict[str, list] = {}
    for p in session.execute(select(Player)).scalars():
        by_name.setdefault(norm_name(p.full_name), []).append(p)
    teams = {t.abbreviation: t for t in session.execute(select(Team)).scalars()}
    final = final_this_week(session, year, week) if week else set()

    rows: list[dict] = []
    unmatched: list[str] = []
    for r in roster:
        if r["pos"] == "DST":
            code = (r["team"] or r["player"]).upper()
            team = teams.get(r["player"].upper()) or teams.get(code)
            if team is None:
                unmatched.append(r["player"])
                continue
            rows.append({**dst_usage(session, team, year, week),
                         "locked": team.abbreviation in final})
            continue
        cands = by_name.get(norm_name(r["player"]), [])
        if not cands:
            unmatched.append(r["player"])
            continue
        games = {p.id: _usage(session, p.id, year, week) for p in cands}
        cands.sort(key=lambda p: (p.position != r["pos"], (p.current_team or "") != r["team"],
                                  not games[p.id], not p.active))
        p = cands[0]
        u = games[p.id]
        rows.append({
            "name": p.full_name, "pos": r["pos"] or p.position,
            "team": p.current_team or r["team"], "roster_team": r["team"],
            "roster_status": p.status, "locked": (p.current_team or r["team"]) in final,
            **_summarise(u),
        })
    return rows, unmatched


def _usage(session: Session, player_id: int, year: int, before_week: int | None = None) -> list[dict]:
    from .models import Game, PlayerGameStats
    from .scoring import score_stats

    q = (select(PlayerGameStats, Game.week).join(Game, PlayerGameStats.game_id == Game.id)
         .where(PlayerGameStats.player_id == player_id, Game.season_year == year))
    out = []
    for st, wk in session.execute(_played(q, Game, before_week).order_by(Game.week)).all():
        out.append({"wk": wk, "tgt": st.targets or 0, "car": st.rush_attempts or 0,
                    "pts": round(score_stats(st), 1)})
    return out


def dst_usage(session: Session, team, year: int, before_week: int | None = None) -> dict:
    from .models import Game, TeamGameStats
    from .scoring import score_team_defense

    q = (select(TeamGameStats, Game.week).join(Game, TeamGameStats.game_id == Game.id)
         .where(TeamGameStats.team_id == team.id, Game.season_year == year))
    games = [{"wk": wk, "tgt": None, "car": None, "pts": round(score_team_defense(tg), 1)}
             for tg, wk in session.execute(_played(q, Game, before_week).order_by(Game.week)).all()]
    return {"name": team.abbreviation, "pos": "DST", "team": team.abbreviation,
            "roster_team": team.abbreviation, "roster_status": None, **_summarise(games)}


def _summarise(games: list[dict]) -> dict:
    pts = [g["pts"] for g in games]
    return {"games": len(games),
            "season_ppg": round(sum(pts) / len(pts), 2) if pts else None,
            "last3": games[-3:]}


# --------------------------------------------------------------------------
# The model (port of docs/app.js modelProj - keep the two in lockstep)
# --------------------------------------------------------------------------

def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _avg(vals) -> float | None:
    vals = [v for v in vals if v is not None and v != ""]
    return sum(vals) / len(vals) if vals else None


def model_ppg(season_ppg: float | None, last3: list[dict], games: int) -> float | None:
    """Season PPG blended with last-3 PPG (``RECENT_FORM_WEIGHT``) from 3 games on."""
    if season_ppg is None:
        return None
    if games >= RECENT_FORM_MIN_GAMES and len(last3) >= RECENT_FORM_MIN_GAMES:
        recent = sum(g["pts"] for g in last3[-3:]) / 3
        return (1 - RECENT_FORM_WEIGHT) * season_ppg + RECENT_FORM_WEIGHT * recent
    return season_ppg


class Model:
    """``modelProj`` from docs/app.js over one sidecar payload."""

    def __init__(self, payload: dict):
        self.V = payload
        self.teams = payload.get("teams") or {}
        self.baseline = payload.get("baseline") or {}
        self.vegas = payload.get("vegas") or {}
        self.w_now = min((payload.get("through_week") or 1) / 6, 0.9)
        self.avg_it = _avg([v.get("it") for v in self.vegas.values()]) if self.vegas else None
        self.avg_opl = _avg([(p.get("opl") if p else None)
                             for p in (self.pace_of(d) for d in self.teams)])
        self.dvp_avg = {pos: _avg([self.dvp_blend(d, pos) for d in self.teams])
                        for pos in (payload.get("positions") or [])}

    def pace_of(self, d: str) -> dict | None:
        t = self.teams.get(d) or {}
        b = self.baseline.get(d) or {}
        if (self.V.get("through_week") or 0) >= 4:
            return t.get("pace") or b.get("pace") or None
        return b.get("pace") or t.get("pace") or None

    def dvp_blend(self, d: str, pos: str) -> float | None:
        t = self.teams.get(d)
        if not t or t.get(pos) is None:
            return None
        b = (self.baseline.get(d) or {}).get(pos) if self.baseline.get(d) else None
        return self.w_now * t[pos] + (1 - self.w_now) * b if b is not None else t[pos]

    def proj(self, ppg: float | None, pos: str, team: str, d: str | None) -> float | None:
        if not ppg:
            return None
        vg = self.vegas.get(team)
        v_f = _clamp((vg.get("it") or 0) / self.avg_it, 0.7, 1.35) if (vg and self.avg_it) else 1
        if pos == "K":
            return ppg * v_f ** 0.8
        if pos == "DST":
            og = self.vegas.get(d) if d else None
            d_f = (_clamp(self.avg_it / og["it"], 0.7, 1.35)
                   if (og and og.get("it") and self.avg_it) else 1)
            return ppg * d_f ** 0.8
        blended = self.dvp_blend(d, pos) if d else None
        avg = self.dvp_avg.get(pos)
        dvp_f = _clamp(blended / avg, 0.75, 1.3) if (blended is not None and avg) else 1
        p = self.pace_of(d) if d else None
        p_f = _clamp(p["opl"] / self.avg_opl, 0.85, 1.15) if (p and p.get("opl") and self.avg_opl) else 1
        return ppg * v_f ** 0.45 * dvp_f ** 0.35 * p_f ** 0.2


# --------------------------------------------------------------------------
# Lineup
# --------------------------------------------------------------------------

def _official_index(official: list[dict]) -> dict:
    idx: dict = {}
    for o in official or []:
        idx.setdefault(norm_name(o.get("n", "")), []).append(o)
    return idx


def _official_for(idx: dict, name: str, team: str) -> dict | None:
    hits = idx.get(norm_name(name)) or []
    same = [o for o in hits if (o.get("tm") or "") == team]
    return (same or hits or [None])[0]


def evaluate(payload: dict, rows: list[dict], injuries: list[dict] | None = None) -> list[dict]:
    """Annotate each roster row with opp, profile, Vegas, projection and availability."""
    model = Model(payload)
    opp = payload.get("opp") or {}
    profiles = payload.get("profiles") or {}
    vegas = payload.get("vegas") or {}
    flags = payload.get("flags") or {}
    off = _official_index(payload.get("official") or [])
    notes = {norm_name(i.get("n", "")): i for i in (injuries or [])}
    out = []
    for r in rows:
        e = dict(r)
        team, pos = e["team"], e["pos"]
        o = opp.get(team) or ""
        d = re.sub(r"^[v@]", "", o) or None
        e["opp"] = o or "BYE"
        e["d"] = d
        prof = profiles.get(d) or {}
        e["rk"] = (prof.get("rk") or {}).get(pos)
        e["rk3"] = (prof.get("rk3") or {}).get(pos)
        e["trend"] = (prof.get("trend") or {}).get(pos)
        e["it"] = (vegas.get(team) or {}).get("it")
        e["opp_it"] = (vegas.get(d) or {}).get("it") if d else None
        e["ppg"] = model_ppg(e.get("season_ppg"), e.get("last3") or [], e.get("games") or 0)
        p = model.proj(e["ppg"], pos, team, d)
        e["proj"] = round(p, 1) if p is not None else None
        e["def_flags"] = [f"{'▼' if f.get('w') == 'out' else '▲'} {f.get('n')} {f.get('w')}"
                          + (" (report)" if f.get("src") == "rpt" else "")
                          for f in (flags.get(d) or [])] if pos not in ("K", "DST") else []
        off_row = None if pos == "DST" else _official_for(off, e["name"], team)
        e["official"] = off_row["st"] if off_row else None
        e["injury"] = off_row.get("inj") if off_row else None
        note = notes.get(norm_name(e["name"]))
        e["note"] = (f"{note.get('d', '')} {note.get('st', '')}: {note.get('inj', '')}"
                     + (f" - {note['tl']}" if note.get("tl") else "")) if note else None
        e["unavailable"] = None
        e["flag"] = None
        if not o:
            e["unavailable"] = "bye"
        elif e["official"] in EXCLUDE_STATUSES:
            e["unavailable"] = e["official"] + (f" ({e['injury']})" if e["injury"] else "")
        elif (e.get("roster_status") or "").upper() == "RES":
            e["unavailable"] = "IR (roster status RES)"
        elif e["official"] in FLAG_STATUSES:
            e["flag"] = e["official"] + (f" ({e['injury']})" if e["injury"] else "")
        if e.get("roster_team") and e["roster_team"] != team and pos != "DST":
            e["team_note"] = f"roster.csv says {e['roster_team']}, DB says {team}"
        out.append(e)
    return out


def _key(e: dict) -> tuple:
    return (e["proj"] is None, -(e["proj"] or 0), -(e.get("season_ppg") or 0), e["name"])


def fill(entries: list[dict]) -> dict[str, dict | None]:
    """Greedy fill by projection; FLEX takes the best remaining RB/WR/TE."""
    pool = sorted((e for e in entries if not e["unavailable"]), key=_key)
    used: set[str] = set()
    out: dict[str, dict | None] = {}
    for slot, elig in SLOTS:
        pick = next((e for e in pool if e["pos"] in elig and e["name"] not in used), None)
        out[slot] = pick
        if pick:
            used.add(pick["name"])
    return out


def build_lineup(payload: dict, rows: list[dict], injuries: list[dict] | None = None,
                 week: int | None = None) -> dict:
    """Pure: sidecar payload + usage rows (+ injury sheet) -> the lineup answer."""
    entries = evaluate(payload, rows, injuries)
    starters = fill(entries)
    used = {e["name"] for e in starters.values() if e}
    bench = sorted((e for e in entries if not e["unavailable"] and e["name"] not in used), key=_key)
    slots = []
    for slot, elig in SLOTS:
        alts = [e for e in bench if e["pos"] in elig][:ALTERNATIVES]
        slots.append({"slot": slot, "starter": starters[slot], "alternatives": alts})

    watch = []
    for s in slots:
        st = s["starter"]
        if not st or not st["flag"]:
            continue
        without = fill([e for e in entries if e["name"] != st["name"]])
        new = [e["name"] for e in without.values() if e and e["name"] not in used]
        st["next_man_up"] = new[0] if new else None
        watch.append({"slot": s["slot"], "player": st["name"], "status": st["official"],
                      "next_man_up": st["next_man_up"]})

    return {
        "year": payload.get("year"),
        "week": week or payload.get("week"),
        "through_week": payload.get("through_week"),
        "slots": slots,
        "unavailable": [{"player": e["name"], "pos": e["pos"], "team": e["team"],
                         "reason": e["unavailable"]} for e in entries if e["unavailable"]],
        "watch": watch,
        "notes": [{"player": e["name"], "note": e["note"]} for e in entries if e["note"]]
                 + [{"player": e["name"], "note": e["team_note"]} for e in entries if e.get("team_note")],
        "snaps": ("snap counts: not available (not in the DB or the sidecar) - "
                  "check `cli usage` / the snap file before a role claim"),
        "recent_form_weight": RECENT_FORM_WEIGHT,
    }


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------

_SLIM = ("name", "pos", "team", "opp", "rk", "rk3", "trend", "it", "opp_it", "season_ppg",
         "ppg", "games", "last3", "proj", "official", "flag", "locked", "def_flags", "next_man_up")


def _slim(e: dict | None) -> dict | None:
    if e is None:
        return None
    return {k: e.get(k) for k in _SLIM}


def to_json(result: dict) -> str:
    out = dict(result)
    out["slots"] = [{"slot": s["slot"], "starter": _slim(s["starter"]),
                     "alternatives": [_slim(a) for a in s["alternatives"]]} for s in result["slots"]]
    return json.dumps(out, indent=2)


def _last3(e: dict) -> str:
    g = e.get("last3") or []
    if not g:
        return "no games"
    pts = "/".join(f"{x['pts']:.1f}" for x in g)
    if e["pos"] in ("K", "DST"):
        return f"pts {pts}"
    if e["pos"] == "QB":
        return f"car {'/'.join(str(x['car']) for x in g)} · pts {pts}"
    return (f"tgt {'/'.join(str(x['tgt']) for x in g)} · car {'/'.join(str(x['car']) for x in g)}"
            f" · pts {pts}")


def _row(e: dict, role: str) -> str:
    rk = lambda v: "" if v is None else str(v)  # noqa: E731
    tr = "" if e.get("trend") is None else f"{e['trend']:+d}"
    it = e.get("it")
    if e["pos"] == "DST":
        it_s = f"opp {e['opp_it']:.1f}" if e.get("opp_it") is not None else ""
    else:
        it_s = f"{it:.1f}" if it is not None else ""
    lock = "locked (game final)" if e.get("locked") else None
    flag = "; ".join(x for x in ([lock, e.get("flag")] + (e.get("def_flags") or [])) if x)
    if e.get("next_man_up"):
        flag += f"; next man up: {e['next_man_up']}"
    proj = f"**{e['proj']:.1f}**" if e.get("proj") is not None and role == "start" else (
        f"{e['proj']:.1f}" if e.get("proj") is not None else "n/a")
    return (f"| {role} | {e['name']} ({e['pos']}, {e['team']}) | {e['opp']} | {rk(e.get('rk'))} / "
            f"{rk(e.get('rk3'))} / {tr} | {it_s} | {_last3(e)} | {proj} | {flag} |")


def render_markdown(result: dict) -> str:
    lines = [f"# Lineup - {result['year']} week {result['week']} "
             f"(sidecar through week {result['through_week']})", ""]
    summary = []
    for s in result["slots"]:
        st = s["starter"]
        summary.append(f"{s['slot']} {st['name']} {st['proj'] if st['proj'] is not None else 'n/a'}"
                       if st else f"{s['slot']} (empty)")
    lines += ["**Start:** " + " · ".join(summary), ""]
    for s in result["slots"]:
        lines += [f"## {s['slot']}", "",
                  "| | Player | Opp | Rk season / last-3 / trend | Implied | Last 3 | Proj | Flag |",
                  "|---|---|---|---|---|---|---|---|"]
        if s["starter"]:
            lines.append(_row(s["starter"], "start"))
        else:
            lines.append("| start | (nobody available) | | | | | | |")
        for a in s["alternatives"]:
            lines.append(_row(a, "alt"))
        lines.append("")
    lines.append("## Unavailable")
    lines += ([f"- {u['player']} ({u['pos']}, {u['team']}): {u['reason']}" for u in result["unavailable"]]
              or ["- none"])
    lines.append("")
    watch = "; ".join(f"{w['player']} ({w['status']}, {w['slot']}) -> "
                      f"{w['next_man_up'] or 'no healthy replacement'} if he sits"
                      for w in result["watch"]) or "none"
    lines += [f"**Decisions to watch:** {watch}", ""]
    if result["notes"]:
        lines.append("## Notes (injuries.csv, newest row per player)")
        lines += [f"- {n['player']}: {n['note']}" for n in result["notes"]]
        lines.append("")
    lines.append(
        f"_Proj = app matchup model (ppg x vegas^.45 x dvp^.35 x pace^.2; K own implied, "
        f"DST opponent's inverted); ppg = season PPG blended {int(100 * result['recent_form_weight'])}% "
        f"with last-3 PPG from 3 games. Rk: 1 = toughest, 32 = softest; trend = last-3 rank minus "
        f"season rank (+ = softer lately). {result['snaps']}._")
    return "\n".join(lines)
