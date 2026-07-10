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
    #: Market-behavior price ceilings per position. Raw VOR overprices elite
    #: QBs in a 1-QB league (huge margin over QB12, but nobody actually bids
    #: RB1 money on a QB); the cap encodes what the room will really pay, and
    #: the excess dollars flow back to the uncapped positions.
    price_caps: dict[str, float] = field(default_factory=lambda: {"QB": 30.0})

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
    team: str           # current NFL team (abbr); "" if unknown
    games: int          # games in the most recent season
    total: float        # last-season total fantasy points
    ppg: float          # last-season points per game
    w3yr: float         # weighted 3-year average of season totals
    basis_value: float  # the value used for pricing (per --basis)
    tier: int           # effective tier (manual override if given, else k-means)
    kmeans_tier: int
    vor: float          # value over replacement (can be negative)
    dollars: float      # auction value (>= 1)
    pos_rank: int       # rank within position by value
    overall_rank: int   # rank across all positions by value
    is_rookie: bool     # no prior stats (placeholder until tiered by hand)


# --- Value: gather season totals -------------------------------------------


def _latest_season(session: Session) -> int | None:
    return session.scalar(
        select(func.max(Game.season_year)).where(Game.season_type == "regular")
    )


def _active_players(session: Session) -> dict[str, tuple[str, str, str, int | None]]:
    """{key: (name, position, current_team, rookie_year)} for active offensive players."""
    rows = session.execute(
        select(Player.slug, Player.full_name, Player.position, Player.current_team, Player.rookie_year)
        .where(Player.active == 1)
    )
    return {
        f"p{slug}": (name, pos, team or "", rk)
        for slug, name, pos, team, rk in rows
        if pos in OFFENSE_POSITIONS and slug
    }


def _player_entities(session: Session, years: list[int], rules: ScoringRules) -> dict[str, dict]:
    """{key: entity} for offensive players, with per-season (games, points).

    Keyed on the player's stable nflverse id (slug) so keys survive DB rebuilds.
    """
    expr = score_expression(rules)
    query = (
        select(
            Player.slug,
            Player.full_name,
            Player.position,
            Game.season_year,
            func.count(PlayerGameStats.id),
            func.sum(expr),
        )
        .join(PlayerGameStats, PlayerGameStats.player_id == Player.id)
        .join(Game, Game.id == PlayerGameStats.game_id)
        .where(Game.season_year.in_(years), Game.season_type == "regular")
        .group_by(Player.slug, Game.season_year)
    )
    entities: dict[str, dict] = {}
    for slug, name, pos, year, games, points in session.execute(query):
        if pos not in OFFENSE_POSITIONS or not slug:
            continue
        key = f"p{slug}"
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
    # Display floor: a negative average (possible for DSTs and deep bench guys)
    # reads as noise on a draft board, so PPG never goes below zero.
    ppg = max(points / games, 0.0) if games else 0.0
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


# --- Tiers -----------------------------------------------------------------

#: Max players per tier (the lowest two tiers are exempt - they absorb the rest).
MAX_TIER_SIZE = 7


def assign_sized_tiers(ranked_keys: list[str], values: list[float], k: int,
                       max_size: int = MAX_TIER_SIZE) -> dict[str, int]:
    """Tier a best->worst ranked list using value gaps, capped at ``max_size``.

    Starts a new tier at a notable drop in value (so elite players break out into
    their own small tiers), while never exceeding ``max_size`` per tier. The top
    ``k-2`` tiers are formed this way; everyone below is split across the last two
    (uncapped) tiers, so the deep guys don't bloat the meaningful tiers.
    """
    out: dict[str, int] = {}
    n = len(ranked_keys)
    if n == 0:
        return out
    capped = max(k - 2, 1)
    gaps = [values[j] - values[j + 1] for j in range(n - 1)]
    positive = sorted(g for g in gaps if g > 0)
    # A "notable" drop = top quartile of gaps, but never less than 5% of the
    # position's full spread. The long smooth tail makes the quartile tiny; a
    # 2-3% dip between near-equals (Gibbs vs Bijan) must not split a tier.
    spread = values[0] - values[-1] if n > 1 else 0.0
    thr = positive[int(0.75 * (len(positive) - 1))] if positive else float("inf")
    thr = max(thr, 0.05 * spread)
    # Tolerance: gaps that are equal by construction (ratings redistributed to
    # fit pinned tiers) jitter by ~1e-13 in float math; without it the quartile
    # threshold would split some of those boundaries but not others.
    thr -= 1e-6

    tier = 1
    count = 0
    i = 0
    while i < n:
        out[ranked_keys[i]] = tier
        count += 1
        last_capped = tier >= capped
        gap_break = i < n - 1 and gaps[i] >= thr
        i += 1
        if last_capped:
            if count >= max_size:
                break  # fill the last capped tier, then dump the rest
        elif count >= max_size or gap_break:
            tier += 1
            count = 0
    rest = ranked_keys[i:]
    if rest:
        half = (len(rest) + 1) // 2
        for j, key in enumerate(rest):
            out[key] = tier + 1 + (0 if j < half else 1)
    return out


def group_manual_tiers(ordered: list[dict], manual_tiers: dict[str, int],
                       max_size: int = MAX_TIER_SIZE) -> dict[str, int]:
    """Effective tiers that **honor** the manual grouping.

    Players the user placed in the same manual tier stay together (the tiers are
    densely renumbered 1..n by manual order), so the tool never re-splits a
    deliberate grouping by value gaps. A manual tier is only broken up when it
    exceeds ``max_size`` (split into consecutive sub-tiers); the lowest two
    manual tiers are exempt from the cap so the deep field can absorb the rest.
    ``ordered`` must already be sorted by (manual tier, -value); entities with no
    manual tier sort last and are treated as the uncapped bottom.
    """
    out: dict[str, int] = {}
    if not ordered:
        return out
    # The two deepest manual tiers (plus the unmanaged tail) stay uncapped.
    manual_vals = sorted({manual_tiers[e["key"]] for e in ordered if e["key"] in manual_tiers})
    uncapped = set(manual_vals[-2:])
    eff = 0
    prev: object = object()
    count = 0
    for e in ordered:
        m = manual_tiers.get(e["key"], None)
        capped = m is not None and m not in uncapped
        if m != prev or (capped and count >= max_size):
            eff += 1
            count = 0
            prev = m
        out[e["key"]] = eff
        count += 1
    return out


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


def _assign_prices(
    entities: list[dict], config: LeagueConfig, *, tier_smoothing: float,
    fixed_prices: dict[str, float] | None = None,
) -> None:
    """Compute VOR (smoothed within tier) and the dollar value for each entity.

    Mutates each entity with ``vor`` and ``dollars``. The budget pool, minus a $1
    reserve per roster slot, is distributed across positive smoothed-VOR. Any
    ``fixed_prices`` (expected/market prices, by key) are honored exactly and
    removed from the pool first, so the rest re-price around what's left.
    """
    fixed = {
        e["key"]: max(float(fixed_prices[e["key"]]), 1.0)
        for e in entities
        if fixed_prices and e["key"] in fixed_prices
    }

    # Smooth positive VOR toward the (position, tier) mean to flatten within-tier
    # differences, per the league's "smoothed by tier" pricing choice.
    groups: dict[tuple[str, int], list[dict]] = {}
    for ent in entities:
        if ent["key"] in fixed:
            continue
        groups.setdefault((ent["position"], ent["tier"]), []).append(ent)
    for members in groups.values():
        pos_vor = [max(m["vor"], 0.0) for m in members]
        mean = sum(pos_vor) / len(pos_vor) if pos_vor else 0.0
        for m in members:
            raw = max(m["vor"], 0.0)
            m["_svor"] = tier_smoothing * mean + (1 - tier_smoothing) * raw

    n_rostered = config.teams * config.roster_size
    reserve = max(n_rostered - len(fixed), 0)            # $1 per non-fixed slot
    discretionary = max(config.pool - sum(fixed.values()) - reserve, 0)
    total_svor = sum(e["_svor"] for e in entities if e["key"] not in fixed)
    for ent in entities:
        if ent["key"] in fixed:
            ent["dollars"] = round(fixed[ent["key"]], 1)
        else:
            share = (ent["_svor"] / total_svor) if total_svor > 0 else 0.0
            ent["dollars"] = round(1 + share * discretionary, 1)
        ent.pop("_svor", None)


def _apply_price_caps(entities: list[dict], caps: dict[str, float],
                      pinned: set) -> None:
    """Clamp positions to their market-behavior ceiling; excess flows onward.

    Whatever the capped positions "save" is redistributed across the uncapped,
    unpinned entities in proportion to their price above the $1 floor, so the
    board still spends the league's full budget. Order within every position is
    preserved (a clamp only flattens the top; scaling is proportional).
    """
    caps = {pos: c for pos, c in (caps or {}).items() if c and c > 0}
    if not caps:
        return
    excess = 0.0
    for ent in entities:
        cap = caps.get(ent["position"])
        if cap is not None and ent["key"] not in pinned and ent["dollars"] > cap:
            excess += ent["dollars"] - cap
            ent["dollars"] = round(float(cap), 1)
    if excess <= 0:
        return
    receivers = [e for e in entities
                 if e["position"] not in caps and e["key"] not in pinned]
    weight = sum(max(e["dollars"] - 1.0, 0.0) for e in receivers)
    if weight <= 0:
        return
    for e in receivers:
        share = max(e["dollars"] - 1.0, 0.0) / weight
        e["dollars"] = round(e["dollars"] + excess * share, 1)


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
    active_only: bool | None = None,
    fixed_prices: dict[str, float] | None = None,
) -> dict[str, list[ValueRow]]:
    """Compute tiers and auction values, grouped by position.

    ``basis`` selects which value drives pricing (``total`` / ``ppg`` / ``w3yr``);
    all three are reported regardless. ``manual_tiers`` maps an entity ``key`` to
    a tier and overrides the k-means tier for both display and smoothing.

    When ``active_only`` (auto-enabled if any player is marked active) the pool is
    limited to players on a current NFL roster, and active players without recent
    stats (rookies) are included as $1 placeholders to be tiered by hand.
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

    active = _active_players(session)
    use_active = bool(active) if active_only is None else active_only

    if use_active:
        present = {e["key"] for e in entities if e["position"] in OFFENSE_POSITIONS}
        kept = []
        for e in entities:
            if e["position"] == "DST":
                e["team"] = e["name"]
                kept.append(e)
            elif e["key"] in active:
                e["team"] = active[e["key"]][2]
                kept.append(e)
        # Active players with no stats (rookies / didn't play) -> placeholders.
        for key, (name, pos, team, _rk) in active.items():
            if key not in present:
                ent = {"key": key, "name": name, "position": pos, "team": team, "seasons": {}}
                _summarize(ent, latest)
                ent["basis_value"] = _basis_value(ent, basis)
                kept.append(ent)
        entities = kept
    else:
        entities = [e for e in entities if e["basis_value"] > 0 or e["total"] > 0]
        for e in entities:
            e["team"] = e["name"] if e["position"] == "DST" else ""

    for e in entities:
        e["is_rookie"] = e["position"] != "DST" and not e["seasons"]

    by_position: dict[str, list[dict]] = {}
    for ent in entities:
        by_position.setdefault(ent["position"], []).append(ent)
    for pos, group in by_position.items():
        group.sort(key=lambda e: e["basis_value"], reverse=True)
        k = tier_k.get(pos, 6)
        # Auto tier: rank by value, capped at MAX_TIER_SIZE per tier.
        auto = assign_sized_tiers(
            [e["key"] for e in group], [e["basis_value"] for e in group], k
        )
        # Effective tier: if you've set manual tiers, rank by (your tier, value)
        # and re-bin with the size cap so there are a few tiers of <=7 then two
        # unbounded bottom tiers (rather than many tiny tiers).
        if any(e["key"] in manual_tiers for e in group):
            ordered = sorted(
                group, key=lambda e: (manual_tiers.get(e["key"], 10 ** 9), -e["basis_value"])
            )
            eff = group_manual_tiers(ordered, manual_tiers)
        else:
            eff = auto
        for rank, ent in enumerate(group, 1):
            ent["kmeans_tier"] = auto.get(ent["key"], 1)
            ent["tier"] = eff.get(ent["key"], auto.get(ent["key"], 1))
            ent["pos_rank"] = rank

    replacement = _replacement_values(by_position, config)
    for ent in entities:
        ent["vor"] = ent["basis_value"] - replacement.get(ent["position"], 0.0)
    _assign_prices(entities, config, tier_smoothing=tier_smoothing, fixed_prices=fixed_prices)

    # Prices must follow the tier order: manual tiers can rank players against
    # their raw production, which would otherwise let a lower tier out-price a
    # higher one (e.g. tier-4 QBs at $1 under a $19 tier-5). Keep the computed
    # dollar *distribution* but reassign it along each position's (tier, value)
    # order. Explicit market pins (fixed_prices) stay with their player.
    pinned = set(fixed_prices or ())
    for group in by_position.values():
        order = sorted(group, key=lambda e: (e["tier"], -e["basis_value"]))
        movable = [e for e in order if e["key"] not in pinned]
        pool = sorted((e["dollars"] for e in movable), reverse=True)
        for ent, dollars in zip(movable, pool):
            ent["dollars"] = dollars

    _apply_price_caps(entities, config.price_caps, pinned)

    # Overall rank across all positions, by value.
    for rank, ent in enumerate(sorted(entities, key=lambda e: e["basis_value"], reverse=True), 1):
        ent["overall_rank"] = rank

    result: dict[str, list[ValueRow]] = {}
    for pos in ALL_POSITIONS:
        rows = [
            ValueRow(
                key=e["key"], name=e["name"], position=e["position"], team=e.get("team", ""),
                games=e["games"], total=round(e["total"], 1), ppg=round(e["ppg"], 2),
                w3yr=round(e["w3yr"], 1), basis_value=round(e["basis_value"], 1), tier=e["tier"],
                kmeans_tier=e["kmeans_tier"], vor=round(e["vor"], 1),
                dollars=max(1, round(e["dollars"])),  # bids are whole dollars
                pos_rank=e["pos_rank"], overall_rank=e["overall_rank"], is_rookie=e["is_rookie"],
            )
            for e in by_position.get(pos, [])
        ]
        rows.sort(key=lambda r: r.dollars, reverse=True)
        result[pos] = rows
    return result
