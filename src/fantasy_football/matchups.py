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

Only regular-season weeks feed the table: playoff defenses see atypical
opponents and would skew the per-game averages.
"""

from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Game, Player, PlayerGameStats, Team
from .scoring import DEFAULT_RULES, ScoringRules, score_stats

#: Offensive positions worth a matchup read (K matchups are noise).
DVP_POSITIONS = ("QB", "RB", "WR", "TE")

#: Regular-season cutoff — playoff weeks (19+) never feed the averages.
_REG_WEEKS = 18


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


def write_dvp_js(
    session: Session,
    path: str,
    year: int,
    *,
    baseline_year: int | None = None,
    rules: ScoringRules = DEFAULT_RULES,
) -> str:
    """Write ``docs/dvp.js`` (``window.FF_DVP``) for the Tier Builder app."""
    cur = defense_vs_position(session, year, rules=rules)
    payload = {
        "year": year,
        "through_week": cur["through_week"],
        "teams": cur["teams"],
        "positions": list(DVP_POSITIONS),
    }
    if baseline_year:
        base = defense_vs_position(session, baseline_year, rules=rules)
        if base["teams"]:
            payload["baseline_year"] = baseline_year
            payload["baseline"] = base["teams"]
    wk = next_week(session, year)
    if wk:
        payload["week"] = wk
        payload["opp"] = week_opponents(session, year, wk)
    with open(path, "w") as fh:
        fh.write("// Generated by `fantasy_football dvp` - do not edit by hand.\n")
        fh.write("window.FF_DVP = ")
        json.dump(payload, fh, ensure_ascii=False)
        fh.write(";\n")
    return path
