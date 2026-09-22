"""Defense-vs-position (DvP) matchup tracking for in-season lineup calls.

Every box-score line in the DB is keyed to a game, so "how many fantasy
points does each defense give up to RBs?" is one aggregation away. This
module computes, per defense, the fantasy points allowed **per game** to
each offensive position (QB/RB/WR/TE, under the league's scoring), ranks
the 32 defenses (rank 1 = stingiest, 32 = softest), and pairs the table
with the coming week's schedule so a glance answers "is my player's
matchup a smash spot or a stay-away?".

Surfaces:
- ``dvp`` CLI command — prints a ranked table for a position and/or writes
  ``docs/dvp.js`` (``window.FF_DVP``), a sidecar the Tier Builder app loads
  next to ``data.js``. It is generated independently of the master-tiers
  rebuild cycle (a weekly Action refreshes it after Monday night's stats
  land) and the app degrades gracefully when the file is absent or stale.
- A ``baseline`` year (normally the prior season) rides along so the first
  few weeks — when one bad Sunday still swings a rank by ten spots — can be
  read against a full-season sample.
- ``personnel_flags`` (from nflverse snap counts) marks defenses whose core
  defenders went missing or came back in the latest week — the two ways a
  points-allowed rank goes quietly stale. The app pairs this with a
  client-side "funnel" badge (WR rank and TE rank far apart = the defense
  chooses where passes go, which is scheme and therefore sticky).

Only regular-season weeks feed the table: playoff defenses see atypical
opponents and would skew the per-game averages.
"""

from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Game, Player, PlayerGameStats, Team, TeamGameStats
from .scoring import DEFAULT_RULES, ScoringRules, score_stats

#: Offensive positions worth a matchup read (K matchups are noise).
DVP_POSITIONS = ("QB", "RB", "WR", "TE")

#: Regular-season cutoff — playoff weeks (19+) never feed the averages.
_REG_WEEKS = 18

#: nflverse per-player snap participation (defense_pct drives the flags).
SNAP_COUNTS_URL = ("https://github.com/nflverse/nflverse-data/releases/"
                   "download/snap_counts/snap_counts_{year}.csv")

#: A "core" defender plays most of the snaps; below "absent" he effectively
#: didn't play (inactive, or left almost immediately).
CORE_PCT = 0.60
ABSENT_PCT = 0.15


def defense_vs_position(
    session: Session,
    year: int,
    *,
    rules: ScoringRules = DEFAULT_RULES,
    through_week: int | None = None,
) -> dict:
    """Fantasy points allowed per game, by defense and position.

    Returns ``{"through_week": int, "teams": {abbr: {"g": games,
    "QB": ppg, ..., "rk": {"QB": rank, ...}}}}`` where rank 1 allows the
    FEWEST points (toughest matchup) and 32 the most (softest). Defenses
    that haven't played (or the whole table, before week 1) come back
    empty rather than erroring, so callers can no-op early in the season.
    """
    max_week = min(through_week or _REG_WEEKS, _REG_WEEKS)
    rows = session.execute(
        select(Player.position, PlayerGameStats, Game)
        .join(PlayerGameStats, PlayerGameStats.player_id == Player.id)
        .join(Game, PlayerGameStats.game_id == Game.id)
        .where(
            Game.season_year == year,
            Game.week <= max_week,
            Player.position.in_(DVP_POSITIONS),
        )
    ).all()

    abbr = {t.id: t.abbreviation for t in session.execute(select(Team)).scalars()}
    allowed: dict[str, dict[str, float]] = {}
    games: dict[str, set[int]] = {}
    played = 0
    for pos, st, game in rows:
        if st.team_id is None:
            continue
        opp_id = game.away_team_id if st.team_id == game.home_team_id else game.home_team_id
        d = abbr.get(opp_id)
        if not d:
            continue
        allowed.setdefault(d, {p: 0.0 for p in DVP_POSITIONS})
        allowed[d][pos] += score_stats(st, rules)
        games.setdefault(d, set()).add(game.id)
        played = max(played, game.week)

    teams: dict[str, dict] = {}
    for d, sums in allowed.items():
        g = len(games[d]) or 1
        teams[d] = {"g": len(games[d])}
        for pos in DVP_POSITIONS:
            teams[d][pos] = round(sums[pos] / g, 1)
    # Rank each position: 1 = fewest points allowed (toughest).
    for pos in DVP_POSITIONS:
        order = sorted(teams, key=lambda d: teams[d][pos])
        for i, d in enumerate(order):
            teams[d].setdefault("rk", {})[pos] = i + 1
    return {"through_week": played, "teams": teams}


def week_opponents(session: Session, year: int, week: int) -> dict[str, str]:
    """``{team_abbr: "@OPP" | "vOPP"}`` for one week's schedule (byes absent)."""
    abbr = {t.id: t.abbreviation for t in session.execute(select(Team)).scalars()}
    out: dict[str, str] = {}
    for g in session.execute(
        select(Game).where(Game.season_year == year, Game.week == week)
    ).scalars():
        home, away = abbr.get(g.home_team_id), abbr.get(g.away_team_id)
        if home and away:
            out[home] = "v" + away
            out[away] = "@" + home
    return out


def next_week(session: Session, year: int) -> int | None:
    """The coming week: the earliest regular-season week with an unplayed game."""
    weeks = session.execute(
        select(Game.week)
        .where(Game.season_year == year, Game.week <= _REG_WEEKS,
               Game.home_score.is_(None))
        .order_by(Game.week)
    ).scalars().first()
    return weeks


def team_pace(session: Session, year: int) -> dict[str, dict]:
    """Game-environment pace: how many plays a team's OPPONENTS get to run.

    A clock-milking team (long drives, heavy run rate, good defense) shrinks
    the other offense's snap count — fewer plays means fewer fantasy chances
    for anyone facing them, regardless of how soft the positional matchup
    looks. ``opp_plays`` per game captures pace + time of possession + game
    script in one number. Returns ``{abbr: {"opl": opp plays/g, "rk": rank}}``
    with rank 1 = fewest opponent plays (the biggest game-shrinkers).
    """
    abbr = {t.id: t.abbreviation for t in session.execute(select(Team)).scalars()}
    rows = session.execute(
        select(TeamGameStats, Game)
        .join(Game, TeamGameStats.game_id == Game.id)
        .where(Game.season_year == year, Game.week <= _REG_WEEKS)
    ).all()
    by_game: dict[int, list] = {}
    for tg, game in rows:
        by_game.setdefault(tg.game_id, []).append(tg)
    opp_plays: dict[str, list[int]] = {}
    for tgs in by_game.values():
        if len(tgs) != 2:
            continue
        for me, them in (tgs, reversed(tgs)):
            a = abbr.get(me.team_id)
            if a and them.plays:
                opp_plays.setdefault(a, []).append(them.plays)
    out = {a: {"opl": round(sum(v) / len(v), 1)} for a, v in opp_plays.items() if v}
    for i, a in enumerate(sorted(out, key=lambda a: out[a]["opl"])):
        out[a]["rk"] = i + 1
    return out


def personnel_flags(rows, *, through_week: int) -> dict[str, list[dict]]:
    """Flag defenses whose latest game was played with changed personnel.

    A points-allowed rank silently assumes the same eleven keep showing up;
    it goes stale in both directions — a core defender leaving makes the
    defense weaker than its rank, one returning makes it stronger. From the
    snap-count rows (dicts with ``week``, ``player``, ``position``, ``team``,
    ``defense_pct``) the latest played week is compared against each player's
    earlier participation:

    - ``out``: a core defender (avg >= 60% of defensive snaps in his earlier
      games) who was absent (<= 15% or missing) in the latest week.
    - ``back``: a core defender playing the latest week after missing the
      week before it.

    Needs at least two played weeks (week 1 has no baseline; nothing is
    inferred across seasons — offseason roster churn would flag every team).
    Returns ``{team_abbr: [{"n": name, "p": pos, "w": "out"|"back"}, ...]}``
    with at most four flags per team, biggest snap shares first.
    """
    if through_week < 2:
        return {}
    hist: dict[tuple[str, str], dict] = {}
    # Teams with ANY snap rows in the latest week. The snap-count file lags
    # the box scores by about a day, so a Monday-night game can be missing
    # while its stats are already in - without this guard every core
    # defender on that team would be flagged 'out' (the Rams, wk2 2026).
    reported: set[str] = set()
    for r in rows:
        wk = int(r["week"])
        if wk > through_week:
            continue
        if wk == through_week:
            reported.add(str(r["team"]))
        pct = float(r.get("defense_pct") or 0)
        key = (str(r["team"]), str(r["player"]))
        ent = hist.setdefault(key, {"pos": r.get("position", ""), "weeks": {}})
        ent["weeks"][wk] = max(ent["weeks"].get(wk, 0.0), pct)

    out: dict[str, list] = {}
    for (team, player), ent in hist.items():
        if team not in reported:
            continue  # game not in the snap file yet (or a bye) - no verdict
        weeks = ent["weeks"]
        earlier = [p for w, p in weeks.items() if w < through_week and p > ABSENT_PCT]
        if not earlier or max(earlier) < CORE_PCT:
            continue  # never a core defender before the latest week
        avg = sum(earlier) / len(earlier)
        if avg < CORE_PCT:
            continue
        latest = weeks.get(through_week, 0.0)
        prev = weeks.get(through_week - 1, 0.0)
        if latest <= ABSENT_PCT:
            flag = "out"
        elif latest >= CORE_PCT and prev <= ABSENT_PCT:
            flag = "back"
        else:
            continue
        out.setdefault(team, []).append(
            {"n": player, "p": ent["pos"], "w": flag, "_avg": avg})
    for team in out:
        out[team].sort(key=lambda f: -f["_avg"])
        out[team] = [{k: v for k, v in f.items() if k != "_avg"}
                     for f in out[team][:4]]
    return out


#: Snap-count position groups for personnel-package rates.
_DB_POS = {"CB", "DB", "S", "FS", "SS"}
_LB_POS = {"LB", "ILB", "OLB", "MLB"}


def package_rates(rows, *, through_week: int) -> dict[str, dict]:
    """Average defensive backs on the field per snap, from snap counts.

    Summing every DB's ``defense_pct`` in a game gives how many DBs were on
    the field for the average snap — 4.x reads base-heavy, ~5 nickel, 5.5+
    dime-leaning. That is the measured version of scouting lines like
    "ran nickel 73% of the time". Returns ``{abbr: {"db": avg, "lb": avg}}``.
    """
    per_game: dict[tuple[str, int], dict[str, float]] = {}
    for r in rows:
        wk = int(r["week"])
        if wk > through_week:
            continue
        pos = str(r.get("position", "")).upper()
        grp = "db" if pos in _DB_POS else ("lb" if pos in _LB_POS else None)
        if not grp:
            continue
        g = per_game.setdefault((str(r["team"]), wk), {"db": 0.0, "lb": 0.0})
        g[grp] += float(r.get("defense_pct") or 0)
    out: dict[str, dict] = {}
    agg: dict[str, list] = {}
    for (team, _wk), g in per_game.items():
        agg.setdefault(team, []).append(g)
    for team, gs in agg.items():
        out[team] = {
            "db": round(sum(g["db"] for g in gs) / len(gs), 1),
            "lb": round(sum(g["lb"] for g in gs) / len(gs), 1),
        }
    return out


def pressure_metrics(rows) -> dict[str, dict]:
    """Pressure rate per defense, from play-by-play rows.

    Rusher counts (true blitz rate) live in FTN charting, which publishes
    late and gated - so this uses the outcome side the base pbp always has:
    pressure = (sacks + QB hits) / dropbacks. It is the measured shadow of
    "aggressive, blitz-heavy front". ``rows`` are dicts with ``defteam``,
    ``pass``, ``sack``, ``qb_hit``. Returns
    ``{abbr: {"prs": pct, "rkPrs": rank}}``, rank 1 = most disruptive.
    """
    acc: dict[str, dict] = {}
    for r in rows:
        d = str(r.get("defteam") or "")
        if not d or not r.get("pass"):
            continue
        a = acc.setdefault(d, {"db": 0, "hit": 0})
        a["db"] += 1
        if (r.get("sack") or 0) or (r.get("qb_hit") or 0):
            a["hit"] += 1
    out: dict[str, dict] = {}
    for d, a in acc.items():
        if a["db"]:
            out[d] = {"prs": round(100.0 * a["hit"] / a["db"], 1)}
    for i, d in enumerate(sorted(out, key=lambda d: -out[d]["prs"])):
        out[d]["rkPrs"] = i + 1
    return out


def fetch_scheme_pbp(year: int) -> list[dict]:
    """Play-by-play rows for pressure metrics (needs pandas/network)."""
    import pandas as pd

    from .ingest.nflverse import PBP_URL

    cols = ["week", "season_type", "defteam", "pass", "sack", "qb_hit"]
    df = pd.read_parquet(PBP_URL.format(year=year), columns=cols)
    df = df[(df["season_type"] == "REG") & (df["week"] <= _REG_WEEKS)]
    return df.to_dict("records")


def implied_totals(rows, *, week: int) -> dict[str, dict]:
    """Vegas implied team totals for one week, from schedule betting lines.

    The books' consensus is the sharpest public forecast of scoring there
    is - implied total = game total halved, shifted by half the spread
    (``spread_line`` is the home team's expected margin). ``rows`` are
    dicts with ``week``, ``home_team``, ``away_team``, ``spread_line``,
    ``total_line``. Returns ``{abbr: {"it": implied, "gt": game total}}``;
    games with no posted line are skipped.
    """
    out: dict[str, dict] = {}
    for r in rows:
        if int(r.get("week") or 0) != week:
            continue
        total, spread = r.get("total_line"), r.get("spread_line")
        if total is None or spread is None or total != total or spread != spread:
            continue
        home, away = str(r["home_team"]), str(r["away_team"])
        out[home] = {"it": round(total / 2 + spread / 2, 1), "gt": total}
        out[away] = {"it": round(total / 2 - spread / 2, 1), "gt": total}
    return out


def vegas_weeks(rows) -> dict[int, list[dict]]:
    """Every week's games with implied totals, and actual scores once played.

    Same inputs as :func:`implied_totals` plus optional ``home_score`` /
    ``away_score``. Returns ``{week: [{"h", "a", "gt", "sp", "ith", "ita",
    "hs", "as"}, ...]}`` - scores are ``None`` for unplayed games - so the
    app can flip through the season and grade the books against reality.
    """
    def num(v):
        try:
            f = float(v)
        except (TypeError, ValueError):
            return None
        return None if f != f else f

    out: dict[int, list[dict]] = {}
    for r in rows:
        total, spread = num(r.get("total_line")), num(r.get("spread_line"))
        if total is None or spread is None:
            continue
        wk = int(r.get("week") or 0)
        if not 1 <= wk <= _REG_WEEKS:
            continue
        hs, as_ = num(r.get("home_score")), num(r.get("away_score"))
        out.setdefault(wk, []).append({
            "h": str(r["home_team"]), "a": str(r["away_team"]),
            "gt": total, "sp": spread,
            "ith": round(total / 2 + spread / 2, 1),
            "ita": round(total / 2 - spread / 2, 1),
            "hs": int(hs) if hs is not None else None,
            "as": int(as_) if as_ is not None else None,
        })
    return out


def fetch_vegas(year: int) -> list[dict]:
    """Schedule rows with betting lines and results (needs pandas/network)."""
    import pandas as pd

    from .ingest.nflverse import GAMES_URL

    df = pd.read_csv(GAMES_URL)
    df = df[df["season"] == year]
    return df[["week", "home_team", "away_team", "spread_line", "total_line",
               "home_score", "away_score"]].to_dict("records")


def read_injuries(path: str) -> list[dict]:
    """Read the weekly injury sweep (``injuries.csv``).

    Columns ``date,player,team,pos,status,injury,timeline,replacement,note``
    - who is hurt, how long, who absorbs the work and what the offense's
    scheme does with it. Written by the scheduled sweep (web research runs
    in the session, not in Actions). Newest first per player; hardened.
    """
    import csv
    import os

    if not path or not os.path.exists(path):
        return []
    seen: set[str] = set()
    out: list[dict] = []
    rows = list(csv.DictReader(open(path, newline="")))[:3000]
    rows.sort(key=lambda r: (r.get("date") or ""), reverse=True)
    for r in rows:
        name = (r.get("player") or "").strip()
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        out.append({
            "d": (r.get("date") or "").strip()[:10], "n": name[:60],
            "tm": (r.get("team") or "").strip().upper()[:3],
            "pos": (r.get("pos") or "").strip().upper()[:5],
            "st": (r.get("status") or "").strip()[:20],
            "inj": (r.get("injury") or "").strip()[:120],
            "tl": (r.get("timeline") or "").strip()[:120],
            "rep": (r.get("replacement") or "").strip()[:200],
            "note": (r.get("note") or "").strip()[:400],
        })
    return out


def read_decisions(path: str) -> list[dict]:
    """Read the pre-registered decision log (``decisions.csv``).

    Columns ``date,week,decision,who,board,override,rationale,outcome,
    verdict`` - every roster call with who made it, what the matchup board
    said at the time, whether the call overrode the board, and how it
    turned out (``hit`` / ``miss`` / ``neutral`` / ``pending``). Kept in the
    repo so the partnership's hit rate is a number, not a feeling, and
    rationale is written BEFORE the games, not after. Hardened like the
    other CSV readers: bad rows skipped, text capped, newest first.
    """
    import csv
    import os

    if not path or not os.path.exists(path):
        return []
    out: list[dict] = []
    with open(path, newline="") as fh:
        for i, row in enumerate(csv.DictReader(fh)):
            if i >= 5000:
                break
            decision = (row.get("decision") or "").strip()
            if not decision:
                continue
            out.append({
                "d": (row.get("date") or "").strip()[:10],
                "wk": (row.get("week") or "").strip()[:8],
                "t": decision[:200],
                "who": (row.get("who") or "").strip().lower()[:20],
                "board": (row.get("board") or "").strip()[:300],
                "ovr": (row.get("override") or "").strip().lower() in ("yes", "y", "true", "1"),
                "why": (row.get("rationale") or "").strip()[:500],
                "out": (row.get("outcome") or "").strip()[:300],
                "v": (row.get("verdict") or "").strip().lower()[:12] or "pending",
            })
    return sorted(out, key=lambda r: r["d"], reverse=True)


def read_defense_notes(path: str) -> dict[str, list[dict]]:
    """Read hand-written defensive scouting notes (``defense_notes.csv``).

    Columns ``team,date,note`` — the durable, qualitative layer the box
    scores can't capture: scheme identity ("73% nickel, brackets outside
    WRs"), coordinator tendencies, why a rank is real or a mirage. Notes
    accumulate over the season; the app shows the latest few per team.
    Untrusted-input hardened: unknown teams pass through (validated against
    the payload downstream by simply not matching), text capped, bad rows
    skipped. Returns ``{TEAM: [{"d": date, "t": note}, ...]}`` newest first.
    """
    import csv
    import os

    if not path or not os.path.exists(path):
        return {}
    notes: dict[str, list[dict]] = {}
    with open(path, newline="") as fh:
        for i, row in enumerate(csv.DictReader(fh)):
            if i >= 2000:
                break
            team = (row.get("team") or "").strip().upper()
            text = (row.get("note") or "").strip()
            if not team or len(team) > 3 or not text:
                continue
            notes.setdefault(team, []).append(
                {"d": (row.get("date") or "").strip()[:10], "t": text[:900]})
    for team in notes:
        notes[team] = sorted(notes[team], key=lambda x: x["d"], reverse=True)[:3]
    return notes


def fetch_snap_counts(year: int) -> list[dict]:
    """Regular-season snap-count rows from nflverse (needs pandas/network).

    Callers treat failures as 'no flags this week' — the matchup table is
    useful without them, so a missing file (early September) or a network
    hiccup must never sink the build.
    """
    import pandas as pd

    df = pd.read_csv(SNAP_COUNTS_URL.format(year=year))
    df = df[df["game_type"] == "REG"]
    return df[["week", "player", "position", "team", "defense_pct"]].to_dict("records")


def write_dvp_js(
    session: Session,
    path: str,
    year: int,
    *,
    baseline_year: int | None = None,
    rules: ScoringRules = DEFAULT_RULES,
    flags: dict[str, list[dict]] | None = None,
    notes: dict[str, list[dict]] | None = None,
    metrics: dict[str, dict] | None = None,
    vegas: dict[str, dict] | None = None,
    vegas_weeks_payload: dict[int, list[dict]] | None = None,
    decisions: list[dict] | None = None,
    injuries: list[dict] | None = None,
) -> str:
    """Write ``docs/dvp.js`` (``window.FF_DVP``) for the Tier Builder app."""
    cur = defense_vs_position(session, year, rules=rules)
    for a, pace in team_pace(session, year).items():
        if a in cur["teams"]:
            cur["teams"][a]["pace"] = pace
    payload = {
        "year": year,
        "through_week": cur["through_week"],
        "teams": cur["teams"],
        "positions": list(DVP_POSITIONS),
    }
    if baseline_year:
        base = defense_vs_position(session, baseline_year, rules=rules)
        if base["teams"]:
            for a, pace in team_pace(session, baseline_year).items():
                if a in base["teams"]:
                    base["teams"][a]["pace"] = pace
            payload["baseline_year"] = baseline_year
            payload["baseline"] = base["teams"]
    wk = next_week(session, year)
    if wk:
        payload["week"] = wk
        payload["opp"] = week_opponents(session, year, wk)
    if flags:
        payload["flags"] = flags
    if notes:
        payload["notes"] = notes
    if metrics:
        payload["metrics"] = metrics
    if vegas:
        payload["vegas"] = vegas
    if vegas_weeks_payload:
        payload["vegasWeeks"] = {str(k): v for k, v in vegas_weeks_payload.items()}
    if decisions:
        payload["decisions"] = decisions
    if injuries:
        payload["injuries"] = injuries
    with open(path, "w") as fh:
        fh.write("// Generated by `fantasy_football dvp` - do not edit by hand.\n")
        fh.write("window.FF_DVP = ")
        json.dump(payload, fh, ensure_ascii=False)
        fh.write(";\n")
    return path
