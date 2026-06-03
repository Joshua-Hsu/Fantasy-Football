"""User ratings: an Elo over head-to-head picks, seeded from auction values.

The comparison game asks a human to pick Player A or Player B. Each pick nudges
both players' **user ratings** (an Elo-style score). Ratings are seeded from the
computed auction value so the game only has to *refine* the order, not build it
from scratch. Tiers are then drawn from the user ratings and fed back into
pricing as the league's "real" tiers.
"""

from __future__ import annotations

import datetime as dt
import random

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import Comparison, UserRating
from .scoring import DEFAULT_RULES, ScoringRules
from .valuation import DEFAULT_LEAGUE, LeagueConfig, compute_values, kmeans_1d

#: Elo scale (a rating gap of this many points ~ 10:1 expected odds) and the
#: per-pick step size. Ratings live in fantasy-point units (the seed), compared
#: only within a position, so the scale is interpreted per position.
ELO_SCALE = 400.0
ELO_K = 24.0


def _expected(rating_a: float, rating_b: float) -> float:
    """Expected score for A against B (probability-like, 0..1)."""
    return 1.0 / (1.0 + 10 ** ((rating_b - rating_a) / ELO_SCALE))


def seed_ratings(
    session: Session,
    *,
    year: int | None = None,
    config: LeagueConfig = DEFAULT_LEAGUE,
    rules: ScoringRules = DEFAULT_RULES,
    basis: str = "w3yr",
    manual_tiers: dict[str, int] | None = None,
    force: bool = False,
) -> int:
    """Create user ratings for every valued entity, seeded from its value.

    ``manual_tiers`` provides a hand-set *starting point*: those players are
    seeded by their tier (within position) rather than by value, so the game
    begins from your ranking — but picks still move them (it's a seed, not a
    lock). Existing ratings are left alone (picks survive) except that manual-tier
    players are re-anchored to the latest given tier. ``force`` resets everything
    to the computed value. Returns rows created.
    """
    manual_tiers = manual_tiers or {}
    values = compute_values(session, year=year, config=config, rules=rules, basis=basis)
    existing = {r.key: r for r in session.scalars(select(UserRating))}

    def seed_value(row) -> float:
        tier = manual_tiers.get(row.key)
        if tier is not None:
            # Tier 1 highest; value as a within-tier tiebreak. Picks can still
            # overcome the ~100-pt gap between tiers over several comparisons.
            return (8 - tier) * 100 + min(row.basis_value, 99) * 0.001
        return float(row.basis_value)

    created = 0
    for rows in values.values():
        for row in rows:
            current = existing.get(row.key)
            if current is None:
                session.add(
                    UserRating(key=row.key, name=row.name, position=row.position,
                               rating=seed_value(row))
                )
                created += 1
            elif force or row.key in manual_tiers:
                current.rating = seed_value(row)
                current.name = row.name
    session.commit()
    return created


def record_pick(session: Session, winner_key: str, loser_key: str) -> tuple[UserRating, UserRating]:
    """Apply one head-to-head result: update both Elo ratings and log it."""
    winner = session.get(UserRating, winner_key)
    loser = session.get(UserRating, loser_key)
    if winner is None or loser is None:
        raise ValueError("both entities must have a user rating before comparison")

    exp_win = _expected(winner.rating, loser.rating)
    winner.rating += ELO_K * (1.0 - exp_win)
    loser.rating += ELO_K * (0.0 - (1.0 - exp_win))
    winner.comparisons += 1
    loser.comparisons += 1
    now = dt.datetime.utcnow()
    winner.updated_at = loser.updated_at = now

    session.add(
        Comparison(position=winner.position, winner_key=winner_key, loser_key=loser_key)
    )
    session.commit()
    return winner, loser


def ranking(session: Session, position: str) -> list[UserRating]:
    """All user ratings for a position, best first."""
    return list(
        session.scalars(
            select(UserRating)
            .where(UserRating.position == position.upper())
            .order_by(UserRating.rating.desc())
        )
    )


def next_matchup(
    session: Session, position: str, *, near: int = 4, rng: random.Random | None = None
) -> tuple[UserRating, UserRating] | None:
    """Pick an informative matchup for a position, or None if fewer than two.

    Strategy: pick A among the least-compared players (that's where input is
    scarce), then B from A's nearest-rated neighbours (that's where a pick
    actually moves the order). A little randomness keeps the game varied.
    """
    rng = rng or random
    pool = ranking(session, position)
    if len(pool) < 2:
        return None

    fewest = min(r.comparisons for r in pool)
    candidates = [r for r in pool if r.comparisons <= fewest + 1]
    a = rng.choice(candidates)

    others = sorted((r for r in pool if r.key != a.key), key=lambda r: abs(r.rating - a.rating))
    b = rng.choice(others[: max(1, near)])
    return a, b


def user_rating_tiers(
    session: Session, tier_k: dict[str, int] | None = None
) -> dict[str, int]:
    """Tiers drawn from user ratings (k-means per position) -> {key: tier}.

    Suitable to pass to :func:`valuation.compute_values` as ``manual_tiers`` so
    pricing is smoothed by the human-derived tiers.
    """
    from .valuation import DEFAULT_TIER_K

    tier_k = {**DEFAULT_TIER_K, **(tier_k or {})}
    by_pos: dict[str, list[UserRating]] = {}
    for r in session.scalars(select(UserRating)):
        by_pos.setdefault(r.position, []).append(r)

    tiers: dict[str, int] = {}
    for pos, members in by_pos.items():
        members.sort(key=lambda r: r.rating, reverse=True)
        labels = kmeans_1d([m.rating for m in members], tier_k.get(pos, 6))
        for member, label in zip(members, labels):
            tiers[member.key] = label
    return tiers


def comparison_count(session: Session, position: str | None = None) -> int:
    """How many head-to-head picks have been recorded (optionally by position)."""
    query = select(func.count(Comparison.id))
    if position is not None:
        query = query.where(Comparison.position == position.upper())
    return session.scalar(query) or 0
