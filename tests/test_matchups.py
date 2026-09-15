"""Tests for defense-vs-position matchup tables (matchups.py)."""

from __future__ import annotations

import datetime as dt
import json

import pytest

from fantasy_football.db import create_db_engine, get_sessionmaker, init_db
from fantasy_football.matchups import (
    defense_vs_position,
    next_week,
    week_opponents,
    write_dvp_js,
)
from fantasy_football.models import Game, Player, PlayerGameStats, Season, Team


@pytest.fixture()
def session():
    engine = create_db_engine(":memory:")
    init_db(engine)
    with get_sessionmaker(engine)() as s:
        yield s


def _seed(session):
    gb = Team(abbreviation="GB", name="Packers")
    chi = Team(abbreviation="CHI", name="Bears")
    det = Team(abbreviation="DET", name="Lions")
    min_ = Team(abbreviation="MIN", name="Vikings")
    session.add_all([gb, chi, det, min_, Season(year=2026)])
    session.flush()
    g1 = Game(season_year=2026, week=1, season_type="regular",
              game_date=dt.date(2026, 9, 13),
              home_team_id=gb.id, away_team_id=chi.id, home_score=30, away_score=10)
    g2 = Game(season_year=2026, week=1, season_type="regular",
              game_date=dt.date(2026, 9, 13),
              home_team_id=det.id, away_team_id=min_.id, home_score=20, away_score=17)
    # Unplayed week 2 game -> the "coming week" for opponent lookups.
    g3 = Game(season_year=2026, week=2, season_type="regular",
              game_date=dt.date(2026, 9, 20),
              home_team_id=chi.id, away_team_id=det.id)
    session.add_all([g1, g2, g3])
    session.flush()

    def stat(name, pos, team, game, **kw):
        p = Player(full_name=name, position=pos, slug=name.lower())
        session.add(p)
        session.flush()
        session.add(PlayerGameStats(player_id=p.id, game_id=game.id,
                                    team_id=team.id, **kw))

    # GB RBs rack up 200 rushing yards on CHI; CHI's lone RB gets 50 on GB.
    stat("rbA", "RB", gb, g1, rush_yards=120, rush_touchdowns=1)
    stat("rbB", "RB", gb, g1, rush_yards=80)
    stat("rbC", "RB", chi, g1, rush_yards=50)
    # WR points flow the other way: CHI's WR torches GB.
    stat("wrA", "WR", chi, g1, targets=10, receptions=8, receiving_yards=140,
         receiving_touchdowns=2)
    stat("wrB", "WR", gb, g1, targets=4, receptions=2, receiving_yards=30)
    # The other game stays quiet so DET/MIN rank as tough defenses.
    stat("rbD", "RB", det, g2, rush_yards=40)
    stat("wrC", "WR", min_, g2, targets=5, receptions=3, receiving_yards=40)
    session.commit()
    return g1, g2, g3


def test_points_allowed_and_ranks(session):
    _seed(session)
    table = defense_vs_position(session, 2026)
    assert table["through_week"] == 1
    teams = table["teams"]
    # CHI allowed both GB RB lines: 200 yds + 1 TD = 26 half-PPR points.
    assert teams["CHI"]["RB"] == pytest.approx(26.0)
    # GB allowed CHI's 50-yard RB day.
    assert teams["GB"]["RB"] == pytest.approx(5.0)
    # Rank 1 = fewest allowed; CHI is the softest RB defense of the four.
    assert teams["CHI"]["rk"]["RB"] == 4
    assert teams["GB"]["RB"] < teams["CHI"]["RB"]
    # WR ranks invert: GB gave up the monster WR line.
    assert teams["GB"]["rk"]["WR"] == 4
    # Every defense played one game.
    assert all(t["g"] == 1 for t in teams.values())


def test_week_opponents_and_next_week(session):
    _seed(session)
    assert next_week(session, 2026) == 2
    opp = week_opponents(session, 2026, 2)
    assert opp == {"CHI": "vDET", "DET": "@CHI"}  # GB/MIN on bye


def test_write_dvp_js(tmp_path, session):
    _seed(session)
    out = tmp_path / "dvp.js"
    write_dvp_js(session, str(out), 2026, baseline_year=2025)
    text = out.read_text()
    assert text.startswith("// Generated")
    payload = json.loads(text.split("window.FF_DVP = ", 1)[1].rstrip().rstrip(";"))
    assert payload["year"] == 2026
    assert payload["week"] == 2
    assert payload["opp"]["CHI"] == "vDET"
    assert payload["teams"]["CHI"]["RB"] == 26.0
    # No 2025 data loaded -> baseline omitted rather than empty.
    assert "baseline" not in payload


def test_empty_season_is_harmless(session):
    table = defense_vs_position(session, 2030)
    assert table == {"through_week": 0, "teams": {}}
    assert next_week(session, 2030) is None
