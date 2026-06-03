"""Fantasy scoring: turn box-score stat lines into fantasy points.

The scoring system is configurable via :class:`ScoringRules` (points awarded per
unit of each stat). Common presets are provided: ``standard``, ``half_ppr`` and
``ppr`` (they differ only in points-per-reception).

A single source of truth -- :func:`_terms` -- maps each rules field to the
corresponding ``PlayerGameStats`` column and its coefficient. Both the
Python-side scorer (:func:`score_stats`, for one object) and the SQL-side scorer
(:func:`score_expression`, for aggregate leaderboards) are built from it, so the
two can never drift apart.

Scoring is a linear combination of counting stats, which is why it can be
expressed equally as a Python sum or a SQL expression.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from typing import Any, NamedTuple

from sqlalchemy import func, literal, select
from sqlalchemy.orm import Session

from .models import Game, Player, PlayerGameStats, Team, TeamGameStats


@dataclass(frozen=True)
class ScoringRules:
    """Points awarded per unit of each stat.

    Defaults are a typical "standard" (non-PPR) league. Yardage values are
    per-yard (e.g. 0.04 = 1 point per 25 passing yards).
    """

    passing_yards: float = 0.04          # 1 pt / 25 yds
    passing_td: float = 4.0
    interception: float = -1.0           # Yahoo default (INT thrown)
    rushing_yards: float = 0.1           # 1 pt / 10 yds
    rushing_td: float = 6.0
    reception: float = 0.0               # PPR knob
    receiving_yards: float = 0.1         # 1 pt / 10 yds
    receiving_td: float = 6.0
    fumble_lost: float = -2.0            # Yahoo default (fumble lost)
    extra_point_made: float = 1.0
    return_td: float = 6.0               # kick + punt return TDs
    # Distance-based field goals (min 2, no max): points per made FG by bucket.
    fg_0_19: float = 2.0
    fg_20_29: float = 2.0
    fg_30_39: float = 3.0
    fg_40_49: float = 4.0
    fg_50_59: float = 5.0
    fg_60_plus: float = 6.0


# Preset systems. half_ppr/ppr differ from standard only in reception value.
STANDARD = ScoringRules()
HALF_PPR = replace(STANDARD, reception=0.5)
PPR = replace(STANDARD, reception=1.0)

PRESETS: dict[str, ScoringRules] = {
    "standard": STANDARD,
    "half_ppr": HALF_PPR,
    "ppr": PPR,
}

#: The league this toolkit targets is Half-PPR, so that is the default scoring
#: everywhere unless a caller passes something else.
DEFAULT_RULES = HALF_PPR


def _terms(rules: ScoringRules) -> dict[str, float]:
    """Map each PlayerGameStats attribute to its scoring coefficient."""
    return {
        "pass_yards": rules.passing_yards,
        "pass_touchdowns": rules.passing_td,
        "interceptions_thrown": rules.interception,
        "rush_yards": rules.rushing_yards,
        "rush_touchdowns": rules.rushing_td,
        "receptions": rules.reception,
        "receiving_yards": rules.receiving_yards,
        "receiving_touchdowns": rules.receiving_td,
        "fumbles_lost": rules.fumble_lost,
        "extra_points_made": rules.extra_point_made,
        "kick_return_touchdowns": rules.return_td,
        "punt_return_touchdowns": rules.return_td,
        # Distance-based FG: linear in the per-bucket made-FG counts.
        "fg_made_0_19": rules.fg_0_19,
        "fg_made_20_29": rules.fg_20_29,
        "fg_made_30_39": rules.fg_30_39,
        "fg_made_40_49": rules.fg_40_49,
        "fg_made_50_59": rules.fg_50_59,
        "fg_made_60_plus": rules.fg_60_plus,
    }


def score_stats(stats: Any, rules: ScoringRules = DEFAULT_RULES) -> float:
    """Fantasy points for a single stat line.

    Works on any object exposing the stat attributes (a ``PlayerGameStats`` row
    or a plain stand-in in tests); missing attributes are treated as 0.
    """
    return round(
        sum(coef * (getattr(stats, attr, 0) or 0) for attr, coef in _terms(rules).items()),
        2,
    )


def score_expression(rules: ScoringRules = DEFAULT_RULES):
    """A SQLAlchemy expression computing per-row fantasy points.

    Suitable for use inside aggregate queries, e.g. ``func.sum(score_expression())``.
    Zero-coefficient terms are dropped to keep the SQL compact.
    """
    expr = None
    for attr, coef in _terms(rules).items():
        if coef == 0:
            continue
        term = getattr(PlayerGameStats, attr) * coef
        expr = term if expr is None else expr + term
    return literal(0.0) if expr is None else expr


class LeaderRow(NamedTuple):
    player: str
    position: str | None
    games: int
    points: float
    points_per_game: float


def season_leaders(
    session: Session,
    year: int,
    rules: ScoringRules = DEFAULT_RULES,
    *,
    season_type: str | None = "regular",
    position: str | None = None,
    limit: int = 25,
) -> list[LeaderRow]:
    """Top fantasy scorers for a season under the given rules.

    Aggregation happens in SQL via :func:`score_expression`. ``season_type``
    defaults to regular season; pass ``None`` to include postseason too.
    """
    points = func.sum(score_expression(rules))
    games = func.count(PlayerGameStats.id)

    query = (
        select(Player.full_name, Player.position, games, points)
        .join(PlayerGameStats, PlayerGameStats.player_id == Player.id)
        .join(Game, Game.id == PlayerGameStats.game_id)
        .where(Game.season_year == year)
        .group_by(Player.id)
        .order_by(points.desc())
        .limit(limit)
    )
    if season_type is not None:
        query = query.where(Game.season_type == season_type)
    if position is not None:
        query = query.where(Player.position == position)

    rows: list[LeaderRow] = []
    for name, pos, n_games, total in session.execute(query):
        total = float(total or 0.0)
        n_games = int(n_games or 0)
        rows.append(
            LeaderRow(
                player=name,
                position=pos,
                games=n_games,
                points=round(total, 2),
                points_per_game=round(total / n_games, 2) if n_games else 0.0,
            )
        )
    return rows


# --- Team defense (DST) ----------------------------------------------------
# DST scoring is *not* a linear function of the stats: points-allowed and
# yards-allowed are scored in tiers. So, unlike offense, it can't be reduced to
# a single SQL coefficient sum -- we score each team-game in Python and total.

#: A tier table is a list of ``(inclusive_upper_bound, points)`` pairs in
#: ascending order; the first bound the value is <= wins. Use ``math.inf`` last.
Tier = tuple[float, float]

DEFAULT_POINTS_ALLOWED_TIERS: list[Tier] = [
    (0, 10), (6, 7), (13, 4), (20, 1), (27, 0), (34, -1), (math.inf, -4),
]
DEFAULT_YARDS_ALLOWED_TIERS: list[Tier] = [
    (99, 5), (199, 3), (299, 2), (349, 0), (399, -1), (449, -3), (math.inf, -5),
]


@dataclass(frozen=True)
class DefenseScoringRules:
    """Points for a team defense's per-game production.

    Event values are per occurrence; points-allowed and yards-allowed are scored
    via the tier tables. Defaults match the confirmed league settings.
    """

    sack: float = 1.0
    interception: float = 2.0
    fumble_recovery: float = 2.0
    touchdown: float = 6.0          # defensive + special-teams TDs
    safety: float = 2.0
    points_allowed_tiers: list[Tier] = field(default_factory=lambda: list(DEFAULT_POINTS_ALLOWED_TIERS))
    yards_allowed_tiers: list[Tier] = field(default_factory=lambda: list(DEFAULT_YARDS_ALLOWED_TIERS))


DEFAULT_DEFENSE_RULES = DefenseScoringRules()


def _tier_points(value: float, tiers: list[Tier]) -> float:
    """Points for ``value`` under a tier table (first inclusive bound it meets)."""
    for upper, points in tiers:
        if value <= upper:
            return points
    return tiers[-1][1] if tiers else 0.0


def score_team_defense(
    stats: Any, rules: DefenseScoringRules = DEFAULT_DEFENSE_RULES
) -> float:
    """Fantasy points for one team-defense game line (a ``TeamGameStats``).

    Works on any object exposing the relevant attributes; missing ones are 0.
    """
    def g(name: str) -> float:
        return getattr(stats, name, 0) or 0

    events = (
        g("sacks") * rules.sack
        + g("interceptions") * rules.interception
        + g("fumble_recoveries") * rules.fumble_recovery
        + (g("defensive_tds") + g("special_teams_tds")) * rules.touchdown
        + g("safeties") * rules.safety
    )
    tiers = _tier_points(g("points_allowed"), rules.points_allowed_tiers) + _tier_points(
        g("yards_allowed"), rules.yards_allowed_tiers
    )
    return round(events + tiers, 2)


def team_defense_season_leaders(
    session: Session,
    year: int,
    rules: DefenseScoringRules = DEFAULT_DEFENSE_RULES,
    *,
    season_type: str | None = "regular",
    limit: int = 25,
) -> list[LeaderRow]:
    """Top scoring team defenses for a season (position reported as ``DST``).

    Aggregates per-game DST points in Python because of the tiered scoring.
    """
    query = (
        select(Team.abbreviation, TeamGameStats)
        .join(TeamGameStats, TeamGameStats.team_id == Team.id)
        .join(Game, Game.id == TeamGameStats.game_id)
        .where(Game.season_year == year)
    )
    if season_type is not None:
        query = query.where(Game.season_type == season_type)

    totals: dict[str, list[float]] = {}
    for abbr, tgs in session.execute(query):
        totals.setdefault(abbr, []).append(score_team_defense(tgs, rules))

    rows = [
        LeaderRow(
            player=abbr,
            position="DST",
            games=len(pts),
            points=round(sum(pts), 2),
            points_per_game=round(sum(pts) / len(pts), 2) if pts else 0.0,
        )
        for abbr, pts in totals.items()
    ]
    rows.sort(key=lambda r: r.points, reverse=True)
    return rows[:limit]
