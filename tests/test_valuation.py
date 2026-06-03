"""Tests for auction valuation: k-means tiers, flex replacement, pricing."""

from __future__ import annotations

import datetime as dt

import pytest

from fantasy_football.db import create_db_engine, get_sessionmaker, init_db
from fantasy_football.models import Game, Player, PlayerGameStats, Season, Team, TeamGameStats
from fantasy_football.valuation import (
    LeagueConfig,
    ValueRow,
    _replacement_values,
    compute_values,
    kmeans_1d,
)


def test_kmeans_orders_tiers_high_to_low():
    values = [100, 98, 60, 58, 10, 8]
    tiers = kmeans_1d(values, 3)
    assert tiers[0] == 1 and tiers[1] == 1     # top cluster
    assert tiers[4] == 3 and tiers[5] == 3     # bottom cluster
    assert set(tiers) == {1, 2, 3}


def test_kmeans_clamps_k_to_distinct_values():
    assert kmeans_1d([5, 5, 5], 4) == [1, 1, 1]
    assert kmeans_1d([], 3) == []


def test_replacement_levels_account_for_flex():
    config = LeagueConfig(
        teams=1, budget=20,
        starters={"QB": 1, "RB": 2, "WR": 2, "TE": 1, "K": 1, "DST": 1},
        flex=1, bench=0,
    )
    by_position = {
        "RB": [{"basis_value": v, "position": "RB"} for v in [100, 90, 80, 70, 60]],
        "WR": [{"basis_value": v, "position": "WR"} for v in [50, 40, 30]],
        "TE": [{"basis_value": v, "position": "TE"} for v in [20, 10]],
    }
    repl = _replacement_values(by_position, config)
    # Best flex candidate is RB#3 (80), so RB replacement moves to rank 3 -> 70.
    assert repl["RB"] == 70
    assert repl["WR"] == 30
    assert repl["TE"] == 10


@pytest.fixture()
def session():
    engine = create_db_engine(":memory:")
    init_db(engine)
    with get_sessionmaker(engine)() as s:
        yield s


def _seed(session):
    gb = Team(abbreviation="GB", name="Packers")
    chi = Team(abbreviation="CHI", name="Bears")
    session.add_all([gb, chi, Season(year=2025)])
    session.flush()
    game = Game(season_year=2025, week=1, season_type="regular",
                game_date=dt.date(2025, 9, 7),
                home_team_id=gb.id, away_team_id=chi.id, home_score=20, away_score=10)
    session.add(game)
    session.flush()

    # A spread of RBs so tiering and replacement have something to work with.
    for i, yds in enumerate([150, 120, 90, 60, 40, 20]):
        p = Player(full_name=f"RB{i}", position="RB", slug=f"rb{i}")
        session.add(p)
        session.flush()
        session.add(PlayerGameStats(player_id=p.id, game_id=game.id, team_id=gb.id,
                                    rush_yards=yds, rush_touchdowns=1 if i < 3 else 0))
    qb = Player(full_name="QB0", position="QB", slug="qb0")
    session.add(qb)
    session.flush()
    session.add(PlayerGameStats(player_id=qb.id, game_id=game.id, team_id=gb.id,
                                pass_yards=300, pass_touchdowns=3))
    session.add_all([
        TeamGameStats(team_id=gb.id, game_id=game.id, is_home=True,
                      points_allowed=10, yards_allowed=250, sacks=4, interceptions=2),
        TeamGameStats(team_id=chi.id, game_id=game.id, is_home=False,
                      points_allowed=20, yards_allowed=380, sacks=1),
    ])
    session.commit()


def test_compute_values_smoke(session):
    _seed(session)
    config = LeagueConfig(teams=1, budget=50)
    values = compute_values(session, year=2025, config=config)

    rbs = values["RB"]
    assert rbs, "expected RB rows"
    assert all(isinstance(r, ValueRow) for r in rbs)
    # Sorted by dollars descending, and every value is at least $1.
    assert rbs == sorted(rbs, key=lambda r: r.dollars, reverse=True)
    assert all(r.dollars >= 1 for r in rbs)
    # The best RB should be the most expensive and in tier 1.
    assert rbs[0].name == "RB0"
    assert rbs[0].tier == 1
    # With a single loaded season, the weighted 3yr average equals the total.
    assert rbs[0].w3yr == pytest.approx(rbs[0].total)
    # DST got valued too.
    assert values["DST"], "expected DST rows"


def test_active_filter_and_rookie_placeholder(session):
    _seed(session)
    # Mark only RB0/RB1 active, plus a rookie with no stats; RB2.. stay inactive.
    from fantasy_football.models import Player

    players = {p.slug: p for p in session.query(Player)}
    players["rb0"].active = True
    players["rb0"].current_team = "GB"
    players["rb1"].active = True
    players["rb1"].current_team = "CHI"
    rookie = Player(full_name="Rook Wide", position="WR", slug="rookie1",
                    active=True, current_team="GB", rookie_year=2025)
    session.add(rookie)
    session.commit()

    values = compute_values(session, year=2025, config=LeagueConfig(teams=1))  # active auto-on
    rb_names = {r.name for r in values["RB"]}
    assert rb_names == {"RB0", "RB1"}              # inactive RBs excluded
    wrs = values["WR"]
    rook = next(r for r in wrs if r.name == "Rook Wide")
    assert rook.is_rookie is True
    assert rook.dollars >= 1                        # placeholder priced at the floor
    assert rook.team == "GB"


def test_manual_tiers_override_kmeans(session):
    _seed(session)
    # Force the top RB into tier 5; its k-means tier should differ.
    top_key = compute_values(session, year=2025, config=LeagueConfig(teams=1))["RB"][0].key
    overridden = compute_values(
        session, year=2025, config=LeagueConfig(teams=1), manual_tiers={top_key: 5}
    )
    row = next(r for r in overridden["RB"] if r.key == top_key)
    assert row.tier == 5
    assert row.kmeans_tier == 1  # automated rating still recorded
