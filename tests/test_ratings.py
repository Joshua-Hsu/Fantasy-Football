"""Tests for user ratings (Elo), matchups, and the comparison web app."""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import func, select

from fantasy_football.db import create_db_engine, get_sessionmaker, init_db
from fantasy_football.models import Comparison, Game, Player, PlayerGameStats, Season, Team, UserRating
from fantasy_football.ratings import (
    _expected,
    next_matchup,
    record_pick,
    seed_ratings,
    user_rating_tiers,
)
from fantasy_football.valuation import LeagueConfig


@pytest.fixture()
def session():
    engine = create_db_engine(":memory:")
    init_db(engine)
    with get_sessionmaker(engine)() as s:
        yield s


def _seed_players(session):
    gb = Team(abbreviation="GB", name="Packers")
    chi = Team(abbreviation="CHI", name="Bears")
    session.add_all([gb, chi, Season(year=2025)])
    session.flush()
    game = Game(season_year=2025, week=1, season_type="regular",
                game_date=dt.date(2025, 9, 7),
                home_team_id=gb.id, away_team_id=chi.id, home_score=20, away_score=10)
    session.add(game)
    session.flush()
    for i, yds in enumerate([150, 120, 90, 60, 40]):
        p = Player(full_name=f"RB{i}", position="RB", slug=f"rb{i}")
        session.add(p)
        session.flush()
        session.add(PlayerGameStats(player_id=p.id, game_id=game.id, team_id=gb.id, rush_yards=yds))
    session.commit()


def test_expected_is_symmetric():
    assert _expected(1500, 1500) == pytest.approx(0.5)
    assert _expected(1700, 1500) + _expected(1500, 1700) == pytest.approx(1.0)


def test_seed_then_pick_moves_ratings(session):
    _seed_players(session)
    created = seed_ratings(session)
    assert created == 5  # five RBs got ratings

    # An underdog beats a favorite: underdog rating rises, favorite falls.
    rbs = sorted(session.scalars(select(UserRating)), key=lambda r: r.rating)
    underdog, favorite = rbs[0], rbs[-1]
    u0, f0 = underdog.rating, favorite.rating
    record_pick(session, underdog.key, favorite.key)
    assert underdog.rating > u0
    assert favorite.rating < f0
    assert session.scalar(select(func.count(Comparison.id))) == 1


def test_seed_is_idempotent_and_preserves_picks(session):
    _seed_players(session)
    seed_ratings(session)
    rbs = list(session.scalars(select(UserRating)))
    record_pick(session, rbs[0].key, rbs[1].key)
    n_before = rbs[0].comparisons
    # Re-seeding must not reset existing ratings/comparisons.
    assert seed_ratings(session) == 0
    again = session.get(UserRating, rbs[0].key)
    assert again.comparisons == n_before


def test_next_matchup_same_position_distinct(session):
    _seed_players(session)
    seed_ratings(session)
    a, b = next_matchup(session, "RB")
    assert a.key != b.key
    assert a.position == b.position == "RB"


def test_user_rating_tiers_cover_all(session):
    _seed_players(session)
    seed_ratings(session)
    tiers = user_rating_tiers(session)
    assert set(tiers) == {r.key for r in session.scalars(select(UserRating))}
    assert min(tiers.values()) == 1


def test_web_app_play_and_pick():
    # Build the app against a fresh on-disk-less DB by sharing the engine.
    from fantasy_football import web

    engine = create_db_engine(":memory:")
    init_db(engine)
    Session = get_sessionmaker(engine)
    with Session() as s:
        _seed_players(s)

    # Point create_app at the same in-memory engine via monkeypatching helpers.
    import fantasy_football.web as webmod

    orig_engine = webmod.create_db_engine
    orig_sm = webmod.get_sessionmaker
    webmod.create_db_engine = lambda _db: engine
    webmod.get_sessionmaker = lambda _e: Session
    try:
        app = web.create_app()
    finally:
        webmod.create_db_engine = orig_engine
        webmod.get_sessionmaker = orig_sm

    client = app.test_client()
    assert client.get("/").status_code == 200
    assert client.get("/play/RB").status_code == 200
    assert client.get("/ranking/RB").status_code == 200

    with Session() as s:
        a, b = next_matchup(s, "RB")
        keys = (a.key, b.key)
    resp = client.post("/pick", data={"pos": "RB", "a": keys[0], "b": keys[1], "winner": keys[0]})
    assert resp.status_code == 302  # redirect back to /play/RB
    with Session() as s:
        assert s.scalar(select(func.count(Comparison.id))) == 1
