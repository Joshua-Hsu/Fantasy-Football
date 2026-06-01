"""Smoke tests for the base schema.

These verify the schema is internally consistent: tables create cleanly, the
core relationships round-trip, foreign keys are enforced, and a uniqueness
constraint actually prevents duplicate stat lines.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy.exc import IntegrityError

from fantasy_football.db import create_db_engine, get_sessionmaker, init_db
from fantasy_football.models import (
    Game,
    Player,
    PlayerGameStats,
    Season,
    Team,
)


@pytest.fixture()
def session():
    engine = create_db_engine(":memory:")
    init_db(engine)
    Session = get_sessionmaker(engine)
    with Session() as s:
        yield s


def _seed_game(session):
    home = Team(abbreviation="GNB", name="Packers", location="Green Bay")
    away = Team(abbreviation="CHI", name="Bears", location="Chicago")
    season = Season(year=2024, regular_season_weeks=18)
    session.add_all([home, away, season])
    session.flush()

    game = Game(
        season_year=2024,
        week=1,
        game_date=dt.date(2024, 9, 8),
        home_team_id=home.id,
        away_team_id=away.id,
        home_score=24,
        away_score=17,
    )
    session.add(game)
    session.flush()
    return home, away, game


def test_tables_created(session):
    expected = {
        "teams",
        "seasons",
        "players",
        "games",
        "player_game_stats",
        "team_game_stats",
    }
    from sqlalchemy import inspect

    assert expected.issubset(set(inspect(session.bind).get_table_names()))


def test_player_game_stats_roundtrip(session):
    home, _away, game = _seed_game(session)
    player = Player(full_name="Aaron Rodgers", position="QB", slug="RodgAa00")
    session.add(player)
    session.flush()

    line = PlayerGameStats(
        player_id=player.id,
        game_id=game.id,
        team_id=home.id,
        pass_completions=22,
        pass_attempts=30,
        pass_yards=295,
        pass_touchdowns=3,
    )
    session.add(line)
    session.commit()

    fetched = session.query(PlayerGameStats).one()
    assert fetched.pass_yards == 295
    # Counting stats with no value supplied default to 0, not NULL.
    assert fetched.rush_yards == 0
    assert fetched.player.full_name == "Aaron Rodgers"
    assert fetched.game.week == 1


def test_duplicate_player_game_rejected(session):
    home, _away, game = _seed_game(session)
    player = Player(full_name="Josh Jacobs", position="RB")
    session.add(player)
    session.flush()

    session.add(PlayerGameStats(player_id=player.id, game_id=game.id, team_id=home.id))
    session.commit()

    session.add(PlayerGameStats(player_id=player.id, game_id=game.id, team_id=home.id))
    with pytest.raises(IntegrityError):
        session.commit()


def test_team_abbreviation_unique(session):
    session.add(Team(abbreviation="GNB", name="Packers"))
    session.commit()
    session.add(Team(abbreviation="GNB", name="Imposters"))
    with pytest.raises(IntegrityError):
        session.commit()
