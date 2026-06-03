"""Auction values from tiers.

Pipeline (no projection model -- historical production is the value signal):

1. **Value** -- compute each player's / team-defense's fantasy points per season
   under the league scoring, then summarize three ways for comparison:
   last-season total, last-season points-per-game, and a weighted 3-year average.
2. **Tier** -- cluster each position's values into tiers with a 1-D k-means.
   These are an automated *starting* rating; the league's real tiers are a manual
   input (Yahoo base prices + head-to-head preferences) supplied as an override.
3. **Price** -- value over replacement (VOR), with replacement levels derived
   from the 12-team roster (including a proper RB/WR/TE flex pool), scaled to the
   auction budget pool and **smoothed within each tier**.

k-means is implemented in pure Python so this module needs no extra dependency.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import NamedTuple

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import Game, Player, PlayerGameStats, Team, TeamGameStats
from .scoring import (
    DEFAULT_DEFENSE_RULES,
    DEFAULT_RULES,
    DefenseScoringRules,
    ScoringRules,
    score_expression,
    score_team_defense,
)

OFFENSE_POSITIONS = ("QB", "RB", "WR", "TE", "K")
FLEX_POSITIONS = ("RB", "WR", "TE")
ALL_POSITIONS = ("QB", "RB", "WR", "TE", "K", "DST")

#: Default k-means tier counts per position (1 = best tier).
DEFAULT_TIER_K = {"QB": 6, "RB": 8, "WR": 8, "TE": 6, "K": 5, "DST": 6}

#: Projected games used to turn points-per-game into a full-season figure.
PROJECTED_GAMES = 17


@dataclass(frozen=True)
class LeagueConfig:
    """Auction/roster settings that drive replacement levels and the dollar scale."""

    teams: int = 12
    budget: int = 200
    starters: dict[str, int] = field(
        default_factory=lambda: {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "K": 1, "DST": 1}
    )
    flex: int = 1  # FLEX spots per team (RB/WR/TE)
    bench: int = 5

    @property
    def roster_size(self) -> int:
        return sum(self.starters.values()) + self.flex + self.bench

    @property
    def pool(self) -> int:
        return self.teams * self.budget


DEFAULT_LEAGUE = LeagueConfig()


class ValueRow(NamedTuple):
    key: str            # stable id: "p<player_id>" or "d<team_abbr>"
    name: str
    position: str
    games: int          # games in the most recent season
    total: float        # last-season total fantasy points
    ppg: float          # last-season points per game
    w3yr: float         # weighted 3-year average of season totals
    basis_value: float  # the value used for pricing (per --basis)
    tier: int           # effective tier (manual override if given, else k-means)
    kmeans_tier: int
    vor: float          # value over replacement (can be negative)
    dollars: float      # auction value (>= 1)


# --- Value: gather season totals -------------------------------------------


def _latest_season(session: Session) -> int | None:
    return session.scalar(
        select(func.max(Game.season_year)).where(Game.season_type == "regular")
    )


def _player_entities(session: Session, years: list[int], rules: ScoringRules) -> dict[str, dict]:
    """{key: entity} for offensive players, with per-season (games, points)."""
    expr = score_expression(rules)
    query = (
        select(
            Player.id,
            Player.full_name,
            Player.position,
            Game.season_year,
            func.count(PlayerGameStats.id),
            func.sum(expr),
        )
        .join(PlayerGameStats, PlayerGameStats.player_id == Player.id)
        .join(Game, Game.id == PlayerGameStats.game_id)
        .where(Game.season_year.in_(years), Game.season_type == "regular")
        .group_by(Player.id, Game.season_year)
    )
    entities: dict[str, dict] = {}
    for pid, name, pos, year, games, points in session.execute(query):
        if pos not in OFFENSE_POSITIONS:
            continue
        key = f"p{pid}"
        ent = entities.setdefault(
            key, {"key": key, "name": name, "position": pos, "seasons": {}}
        )
        ent["seasons"][int(year)] = (int(games or 0), float(points or 0.0))
    return entities


def _defense_entities(
    session: Session, years: list[int], def_rules: DefenseScoringRules
) -> dict[str, dict]:
    """{key: entity} for team defenses, scored per game in Python (tiered)."""
    query = (
        select(Team.abbreviation, Game.season_year, TeamGameStats)
        .join(TeamGameStats, TeamGameStats.team_id == Team.id)
        .join(Game, Game.id == TeamGameStats.game_id)
        .where(Game.season_year.in_(years), Game.season_type == "regular")
    )
    entities: dict[str, dict] = {}
    for abbr, year, tgs in session.execute(query):
        key = f"d{abbr}"
        ent = entities.setdefault(
            key, {"key": key, "name": abbr, "position": "DST", "seasons": {}}
        )
        games, points = ent["seasons"].get(int(year), (0, 0.0))
        ent["seasons"][int(year)] = (games + 1, points + score_team_defense(tgs, def_rules))
    return entities


def _summarize(entity: dict, latest: int, weights: tuple[int, ...] = (3, 2, 1)) -> dict:
    """Add total / ppg / w3yr summaries to an entity for the latest season."""
    seasons = entity["seasons"]
    games, points = seasons.get(latest, (0, 0.0))
    total = points
    ppg = points / games if games else 0.0
    num = den = 0.0
    for i, w in enumerate(weights):
        yr = latest - i
        if yr in seasons:
            num += w * seasons[yr][1]
            den += w
    w3yr = num / den if den else 0.0
    entity.update(games=games, total=total, ppg=ppg, w3yr=w3yr)
    return entity


def _basis_value(entity: dict, basis: str) -> float:
    if basis == "total":
        return entity["total"]
    if basis == "ppg":
        return entity["ppg"] * PROJECTED_GAMES
    return entity["w3yr"]


# --- Tiers: 1-D k-means ----------------------------------------------------


def kmeans_1d(values: list[float], k: int, *, iters: int = 100) -> list[int]:
    """Cluster ``values`` into ``k`` tiers; returns a tier (1=best) per input value.

    A simple Lloyd's algorithm over one dimension. Centroids are seeded at evenly
    spaced quantiles, then refined. ``k`` is clamped to the number of distinct
    values so degenerate inputs don't produce empty clusters.
    """
    n = len(values)
    if n == 0:
        return []
    distinct = sorted(set(values))
    k = max(1, min(k, len(distinct)))
    if k == 1:
        return [1] * n

    ordered = sorted(values)
    centroids = [ordered[round(i * (n - 1) / (k - 1))] for i in range(k)]
    for _ in range(iters):
        buckets: list[list[float]] = [[] for _ in range(k)]
        for v in ordered:
            j = min(range(k), key=lambda c: abs(v - centroids[c]))
            buckets[j].append(v)
        new = [sum(b) / len(b) if b else centroids[i] for i, b in enumerate(buckets)]
        if new == centroids:
            break
        centroids = new

    # Rank centroids high->low so tier 1 is the most valuable cluster.
    order = sorted(range(k), key=lambda c: centroids[c], reverse=True)
    rank_of = {c: t + 1 for t, c in enumerate(order)}
    return [rank_of[min(range(k), key=lambda c: abs(v - centroids[c]))] for v in values]


# --- Pricing: VOR with flex + tier smoothing -------------------------------


def _replacement_values(
    by_position: dict[str, list[dict]], config: LeagueConfig
) -> dict[str, float]:
    """Replacement value per position, accounting for the RB/WR/TE flex pool.

    ``by_position`` maps position -> entities sorted by basis_value descending.
    Replacement = the value of the first non-starter at that position once flex
    spots are claimed by the best remaining RB/WR/TE.
    """
    base = {pos: config.teams * n for pos, n in config.starters.items()}

    # Flex: best RB/WR/TE beyond their base starters fill the flex spots.
    flex_candidates: list[tuple[float, str]] = []
    for pos in FLEX_POSITIONS:
        for ent in by_position.get(pos, [])[base.get(pos, 0):]:
            flex_candidates.append((ent["basis_value"], pos))
    flex_candidates.sort(reverse=True)
    claimed = {pos: 0 for pos in FLEX_POSITIONS}
    for _value, pos in flex_candidates[: config.teams * config.flex]:
        claimed[pos] += 1

    replacement: dict[str, float] = {}
    for pos, entities in by_position.items():
        rank = base.get(pos, 0) + claimed.get(pos, 0)
        if not entities:
            replacement[pos] = 0.0
        elif rank < len(entities):
            replacement[pos] = entities[rank]["basis_value"]
        else:
            replacement[pos] = entities[-1]["basis_value"]
    return replacement


def _assign_prices(entities: list[dict], config: LeagueConfig, *, tier_smoothing: float) -> None:
    """Compute VOR (smoothed within tier) and the dollar value for each entity.

    Mutates each entity with ``vor`` and ``dollars``. The budget pool, minus a $1
    reserve per roster slot, is distributed across positive smoothed-VOR.
    """
    # Smooth positive VOR toward the (position, tier) mean to flatten within-tier
    # differences, per the league's "smoothed by tier" pricing choice.
    groups: dict[tuple[str, int], list[dict]] = {}
    for ent in entities:
        groups.setdefault((ent["position"], ent["tier"]), []).append(ent)
    for members in groups.values():
        pos_vor = [max(m["vor"], 0.0) for m in members]
        mean = sum(pos_vor) / len(pos_vor) if pos_vor else 0.0
        for m in members:
            raw = max(m["vor"], 0.0)
            m["_svor"] = tier_smoothing * mean + (1 - tier_smoothing) * raw

    total_svor = sum(m["_svor"] for m in entities)
    n_rostered = config.teams * config.roster_size
    discretionary = max(config.pool - n_rostered, 0)
    for ent in entities:
        share = (ent["_svor"] / total_svor) if total_svor > 0 else 0.0
        ent["dollars"] = round(1 + share * discretionary, 1)
        ent.pop("_svor", None)


# --- Top-level API ---------------------------------------------------------


def compute_values(
    session: Session,
    *,
    year: int | None = None,
    config: LeagueConfig = DEFAULT_LEAGUE,
    rules: ScoringRules = DEFAULT_RULES,
    def_rules: DefenseScoringRules = DEFAULT_DEFENSE_RULES,
    basis: str = "w3yr",
    tier_k: dict[str, int] | None = None,
    manual_tiers: dict[str, int] | None = None,
    tier_smoothing: float = 0.5,
) -> dict[str, list[ValueRow]]:
    """Compute tiers and auction values, grouped by position.

    ``basis`` selects which value drives pricing (``total`` / ``ppg`` / ``w3yr``);
    all three are reported regardless. ``manual_tiers`` maps an entity ``key`` to
    a tier and overrides the k-means tier for both display and smoothing.
    """
    latest = year or _latest_season(session)
    if latest is None:
        return {pos: [] for pos in ALL_POSITIONS}
    years = [latest, latest - 1, latest - 2]
    tier_k = {**DEFAULT_TIER_K, **(tier_k or {})}
    manual_tiers = manual_tiers or {}

    entities = list(_player_entities(session, years, rules).values())
    entities += list(_defense_entities(session, years, def_rules).values())

    for ent in entities:
        _summarize(ent, latest)
        ent["basis_value"] = _basis_value(ent, basis)
    # Drop entities with no production in the scoring window.
    entities = [e for e in entities if e["basis_value"] > 0 or e["total"] > 0]

    by_position: dict[str, list[dict]] = {}
    for ent in entities:
        by_position.setdefault(ent["position"], []).append(ent)
    for pos, group in by_position.items():
        group.sort(key=lambda e: e["basis_value"], reverse=True)
        tiers = kmeans_1d([e["basis_value"] for e in group], tier_k.get(pos, 6))
        for ent, kt in zip(group, tiers):
            ent["kmeans_tier"] = kt
            ent["tier"] = manual_tiers.get(ent["key"], kt)

    replacement = _replacement_values(by_position, config)
    for ent in entities:
        ent["vor"] = ent["basis_value"] - replacement.get(ent["position"], 0.0)
    _assign_prices(entities, config, tier_smoothing=tier_smoothing)

    result: dict[str, list[ValueRow]] = {}
    for pos in ALL_POSITIONS:
        rows = [
            ValueRow(
                key=e["key"], name=e["name"], position=e["position"], games=e["games"],
                total=round(e["total"], 1), ppg=round(e["ppg"], 2), w3yr=round(e["w3yr"], 1),
                basis_value=round(e["basis_value"], 1), tier=e["tier"],
                kmeans_tier=e["kmeans_tier"], vor=round(e["vor"], 1), dollars=e["dollars"],
            )
            for e in by_position.get(pos, [])
        ]
        rows.sort(key=lambda r: r.dollars, reverse=True)
        result[pos] = rows
    return result
