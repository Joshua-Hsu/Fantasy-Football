"""Offline tests for the nflverse ingestion mapping and idempotency.

These never touch the network: the four nflverse fetchers are monkeypatched to
return small hand-built DataFrames, so we test the transform/upsert logic in
isolation.
"""

from __future__ import annotations

import pandas as pd
import pytest

from fantasy_football import ingest
from fantasy_football.ingest import nflverse
from fantasy_football.db import create_db_engine, get_sessionmaker, init_db
from fantasy_football.models import Game, Player, PlayerGameStats, Team


TEAMS_DF = pd.DataFrame(
    [
        {"team_abbr": "GB", "team_name": "Green Bay Packers", "team_nick": "Packers",
         "team_conf": "NFC", "team_division": "NFC North"},
        {"team_abbr": "CHI", "team_name": "Chicago Bears", "team_nick": "Bears",
         "team_conf": "NFC", "team_division": "NFC North"},
    ]
)

GAMES_DF = pd.DataFrame(
    [
        {"game_id": "2024_01_CHI_GB", "season": 2024, "game_type": "REG", "week": 1,
         "gameday": "2024-09-08", "home_team": "GB", "away_team": "CHI",
         "home_score": 24, "away_score": 17, "stadium": "Lambeau Field"},
        {"game_id": "2023_20_CHI_GB", "season": 2023, "game_type": "DIV", "week": 20,
         "gameday": "2024-01-20", "home_team": "GB", "away_team": "CHI",
         "home_score": 21, "away_score": 14, "stadium": "Lambeau Field"},
    ]
)

ROSTER_DF = pd.DataFrame(
    [
        {"gsis_id": "00-0000001", "full_name": "Jordan Love", "first_name": "Jordan",
         "last_name": "Love", "position": "QB", "birth_date": "1998-11-02",
         "height": 76, "weight": 219, "college": "Utah State"},
    ]
)

WEEKLY_DF = pd.DataFrame(
    [
        {"player_id": "00-0000001", "player_display_name": "Jordan Love", "position": "QB",
         "season": 2024, "week": 1, "season_type": "REG", "team": "GB", "opponent_team": "CHI",
         "completions": 22, "attempts": 30, "passing_yards": 295, "passing_tds": 3,
         "passing_interceptions": 1, "sacks_suffered": 2, "sack_yards_lost": 14,
         "carries": 3, "rushing_yards": 12, "rushing_tds": 0,
         "targets": 0, "receptions": 0, "receiving_yards": 0, "receiving_tds": 0,
         "sack_fumbles": 1, "sack_fumbles_lost": 0, "rushing_fumbles": 0,
         "rushing_fumbles_lost": 0, "receiving_fumbles": 0, "receiving_fumbles_lost": 0,
         "fg_made": 0, "fg_att": 0, "pat_made": 0, "pat_att": 0,
         "kickoff_return_yards": 0, "punt_return_yards": 0},
        # A player with only a stat line (no roster row) — exercises the fallback.
        {"player_id": "00-0000002", "player_display_name": "Practice Squadder",
         "position": "WR", "season": 2024, "week": 1, "season_type": "REG",
         "team": "CHI", "opponent_team": "GB",
         "completions": 0, "attempts": 0, "passing_yards": 0, "passing_tds": 0,
         "passing_interceptions": 0, "sacks_suffered": 0, "sack_yards_lost": 0,
         "carries": 0, "rushing_yards": 0, "rushing_tds": 0,
         "targets": 4, "receptions": 3, "receiving_yards": 41, "receiving_tds": 1,
         "sack_fumbles": 0, "sack_fumbles_lost": 0, "rushing_fumbles": 0,
         "rushing_fumbles_lost": 0, "receiving_fumbles": 0, "receiving_fumbles_lost": 0,
         "fg_made": 0, "fg_att": 0, "pat_made": 0, "pat_att": 0,
         "kickoff_return_yards": 35, "punt_return_yards": 0},
    ]
)


# Team-week rows for the 2024 GB-CHI game (GB won 24-17).
TEAM_WEEK_DF = pd.DataFrame(
    [
        {"team": "GB", "opponent_team": "CHI", "season": 2024, "week": 1,
         "season_type": "REG", "passing_yards": 295, "rushing_yards": 100,
         "passing_interceptions": 1, "sack_fumbles_lost": 0, "rushing_fumbles_lost": 0,
         "receiving_fumbles_lost": 0, "def_sacks": 3, "def_interceptions": 1,
         "fumble_recovery_opp": 1, "def_tds": 0, "special_teams_tds": 0, "def_safeties": 0},
        {"team": "CHI", "opponent_team": "GB", "season": 2024, "week": 1,
         "season_type": "REG", "passing_yards": 180, "rushing_yards": 80,
         "passing_interceptions": 1, "sack_fumbles_lost": 1, "rushing_fumbles_lost": 0,
         "receiving_fumbles_lost": 0, "def_sacks": 2, "def_interceptions": 1,
         "fumble_recovery_opp": 0, "def_tds": 1, "special_teams_tds": 0, "def_safeties": 0},
    ]
)


@pytest.fixture()
def session(monkeypatch):
    monkeypatch.setattr(nflverse, "_fetch_teams", lambda: TEAMS_DF.copy())
    monkeypatch.setattr(nflverse, "_fetch_games", lambda: GAMES_DF.copy())
    monkeypatch.setattr(nflverse, "_fetch_roster", lambda year: ROSTER_DF.copy())
    monkeypatch.setattr(nflverse, "_fetch_weekly", lambda year: WEEKLY_DF.copy())
    monkeypatch.setattr(nflverse, "_fetch_team_week", lambda year: TEAM_WEEK_DF.copy())

    engine = create_db_engine(":memory:")
    init_db(engine)
    with get_sessionmaker(engine)() as s:
        yield s


def test_load_season_populates_all_tables(session):
    summary = ingest.load_season(session, 2024)
    assert summary["games"] == 1  # only the 2024 game
    assert summary["stat_lines"] == 2
    assert summary["team_lines"] == 2  # one row per team in the game


def test_team_stats_mapping(session):
    from fantasy_football.models import Team, TeamGameStats

    ingest.load_season(session, 2024)
    gb = session.query(Team).filter_by(abbreviation="GB").one()
    tgs = session.query(TeamGameStats).filter_by(team_id=gb.id).one()
    assert tgs.is_home is True or tgs.is_home == 1
    assert tgs.points == 24            # GB's score from the game
    assert tgs.points_allowed == 17    # CHI's score
    assert tgs.total_yards == 395      # 295 pass + 100 rush
    assert tgs.yards_allowed == 260    # CHI offense: 180 + 80
    assert tgs.sacks == 3
    assert tgs.interceptions == 1
    assert tgs.fumble_recoveries == 1
    assert tgs.turnovers == 1          # GB threw 1 INT, lost 0 fumbles

    assert session.query(Team).count() == 2
    # Roster player + fallback stat-only player.
    assert session.query(Player).count() == 2


def test_stat_mapping_is_correct(session):
    ingest.load_season(session, 2024)
    love = session.query(Player).filter_by(slug="00-0000001").one()
    line = session.query(PlayerGameStats).filter_by(player_id=love.id).one()
    assert line.pass_yards == 295
    assert line.pass_touchdowns == 3
    assert line.interceptions_thrown == 1
    assert line.times_sacked == 2
    assert line.fumbles == 1  # summed from split fumble columns
    assert line.rush_yards == 12
    # Player on the GB side is linked to the GB-home game.
    assert line.game.home_team.abbreviation == "GB"


def test_returns_and_receiving_mapping(session):
    ingest.load_season(session, 2024)
    wr = session.query(Player).filter_by(slug="00-0000002").one()
    line = session.query(PlayerGameStats).filter_by(player_id=wr.id).one()
    assert line.receptions == 3
    assert line.receiving_yards == 41
    assert line.receiving_touchdowns == 1
    assert line.kick_return_yards == 35


def test_reload_is_idempotent(session):
    first = ingest.load_season(session, 2024)
    games_before = session.query(Game).count()
    stats_before = session.query(PlayerGameStats).count()

    second = ingest.load_season(session, 2024)
    # No new rows on reload.
    assert session.query(Game).count() == games_before
    assert session.query(PlayerGameStats).count() == stats_before
    assert first["stat_lines"] == second["stat_lines"]


def test_season_type_mapping(session):
    ingest.load_games(session, 2023)
    game = session.query(Game).filter_by(season_year=2023).one()
    assert game.season_type == "postseason"  # DIV -> postseason
    assert game.week == 20
