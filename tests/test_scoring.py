"""Tests for fantasy scoring (offense, kicker, DST).

Pure-logic tests use lightweight stand-in objects; the SQL-vs-Python agreement
test seeds a small in-memory database. No network access.
"""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import pytest

from fantasy_football import scoring
from fantasy_football.db import create_db_engine, get_sessionmaker, init_db
from fantasy_football.models import Game, Player, PlayerGameStats, Season, Team, TeamGameStats
from fantasy_football.scoring import (
    HALF_PPR,
    PPR,
    DefenseScoringRules,
    score_stats,
    score_team_defense,
    season_leaders,
    team_defense_season_leaders,
)


def test_offense_half_ppr_vs_ppr():
    wr = SimpleNamespace(receptions=6, receiving_yards=85, receiving_touchdowns=1)
    assert score_stats(wr, HALF_PPR) == pytest.approx(17.5)  # 3 + 8.5 + 6
    assert score_stats(wr, PPR) == pytest.approx(20.5)       # 6 + 8.5 + 6


def test_passing_default_is_half_ppr():
    qb = SimpleNamespace(pass_yards=300, pass_touchdowns=2, interceptions_thrown=1)
    # 300*0.04 + 2*4 + 1*(-2) = 12 + 8 - 2 = 18
    assert score_stats(qb) == pytest.approx(18.0)


def test_distance_based_field_goals():
    k = SimpleNamespace(
        fg_made_0_19=1, fg_made_50_59=1, fg_made_60_plus=1, extra_points_made=3
    )
    # 2 + 5 + 6 + 3 = 16
    assert score_stats(k) == pytest.approx(16.0)


def test_defense_events_and_tiers():
    dst = SimpleNamespace(
        sacks=4, interceptions=2, fumble_recoveries=1, defensive_tds=1,
        special_teams_tds=0, safeties=0, points_allowed=10, yards_allowed=250,
    )
    # events 4 + 4 + 2 + 6 = 16; PA(10)->4 tier; YA(250)->2 tier; total 22
    assert score_team_defense(dst) == pytest.approx(22.0)


def test_defense_points_allowed_shutout_tier():
    dst = SimpleNamespace(points_allowed=0, yards_allowed=90)
    # PA(0)->10, YA(<100)->5
    assert score_team_defense(dst) == pytest.approx(15.0)


@pytest.fixture()
def session():
    engine = create_db_engine(":memory:")
    init_db(engine)
    with get_sessionmaker(engine)() as s:
        yield s


def _seed(session):
    gb = Team(abbreviation="GB", name="Packers")
    chi = Team(abbreviation="CHI", name="Bears")
    season = Season(year=2024)
    session.add_all([gb, chi, season])
    session.flush()
    game = Game(
        season_year=2024, week=1, season_type="regular",
        game_date=dt.date(2024, 9, 8),
        home_team_id=gb.id, away_team_id=chi.id, home_score=24, away_score=17,
    )
    session.add(game)
    session.flush()
    return gb, chi, game


def test_sql_leaders_match_python_scoring(session):
    gb, chi, game = _seed(session)
    rb = Player(full_name="Josh Jacobs", position="RB", slug="x1")
    wr = Player(full_name="Romeo Doubs", position="WR", slug="x2")
    session.add_all([rb, wr])
    session.flush()
    lines = [
        PlayerGameStats(player_id=rb.id, game_id=game.id, team_id=gb.id,
                        rush_yards=120, rush_touchdowns=1, receptions=3, receiving_yards=20),
        PlayerGameStats(player_id=wr.id, game_id=game.id, team_id=gb.id,
                        receptions=7, receiving_yards=110, receiving_touchdowns=1),
    ]
    session.add_all(lines)
    session.commit()

    leaders = season_leaders(session, 2024, HALF_PPR)
    by_name = {r.player: r.points for r in leaders}
    # SQL aggregate must equal the Python per-line score.
    assert by_name["Josh Jacobs"] == pytest.approx(score_stats(lines[0], HALF_PPR))
    assert by_name["Romeo Doubs"] == pytest.approx(score_stats(lines[1], HALF_PPR))
    # RB: 12 + 6 + 1.5 + 2 = 21.5 ; WR: 3.5 + 11 + 6 = 20.5 -> RB ranks first.
    assert leaders[0].player == "Josh Jacobs"


def test_leaders_position_filter(session):
    gb, chi, game = _seed(session)
    qb = Player(full_name="Jordan Love", position="QB", slug="q1")
    rb = Player(full_name="Josh Jacobs", position="RB", slug="r1")
    session.add_all([qb, rb])
    session.flush()
    session.add_all([
        PlayerGameStats(player_id=qb.id, game_id=game.id, team_id=gb.id, pass_yards=300),
        PlayerGameStats(player_id=rb.id, game_id=game.id, team_id=gb.id, rush_yards=100),
    ])
    session.commit()

    rbs = season_leaders(session, 2024, HALF_PPR, position="RB")
    assert [r.player for r in rbs] == ["Josh Jacobs"]


def test_team_defense_leaders(session):
    gb, chi, game = _seed(session)
    session.add_all([
        TeamGameStats(team_id=gb.id, game_id=game.id, is_home=True,
                      points_allowed=17, yards_allowed=260, sacks=3, interceptions=1,
                      fumble_recoveries=1),
        TeamGameStats(team_id=chi.id, game_id=game.id, is_home=False,
                      points_allowed=24, yards_allowed=395, sacks=2, interceptions=1,
                      defensive_tds=1),
    ])
    session.commit()

    leaders = team_defense_season_leaders(session, 2024)
    assert {r.position for r in leaders} == {"DST"}
    gb_row = next(r for r in leaders if r.player == "GB")
    # GB: sacks 3 + INT 2 + FR 2 = 7 events; PA(17)->1; YA(260)->2 => 10
    assert gb_row.points == pytest.approx(10.0)
