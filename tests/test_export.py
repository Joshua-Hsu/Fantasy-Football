"""Tests for the Excel draft-board export."""

from __future__ import annotations

import datetime as dt

import pytest

from fantasy_football.db import create_db_engine, get_sessionmaker, init_db
from fantasy_football.export import build_board, write_cheatsheet
from fantasy_football.models import Game, Player, PlayerGameStats, Season, Team
from fantasy_football.valuation import LeagueConfig


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
    for i, yds in enumerate([150, 120, 90, 60, 40, 20]):
        p = Player(full_name=f"RB{i}", position="RB", slug=f"rb{i}")
        session.add(p)
        session.flush()
        session.add(PlayerGameStats(player_id=p.id, game_id=game.id, team_id=gb.id, rush_yards=yds))
    session.commit()


def test_build_board_ranks_within_position_and_tier(session):
    _seed(session)
    board = build_board(session, year=2025, config=LeagueConfig(teams=1))
    rbs = board["RB"]
    assert [r.pos_rank for r in rbs] == list(range(1, len(rbs) + 1))
    # Tier ranks restart at 1 within each tier.
    first_in_tier = [r for r in rbs if r.tier_rank == 1]
    assert {r.tier for r in first_in_tier} == set(r.tier for r in rbs)
    assert rbs[0].name == "RB0"  # best RB on top


def test_write_cheatsheet_has_draft_board_and_live_formulas(session, tmp_path):
    _seed(session)
    out = tmp_path / "board.xlsx"
    write_cheatsheet(session, str(out), year=2025, config=LeagueConfig(teams=1))

    from openpyxl import load_workbook

    wb = load_workbook(out)
    assert wb.sheetnames[0] == "Draft Board"   # live sheet first
    assert "RB" in wb.sheetnames               # static position sheet present

    ws = wb["Draft Board"]
    assert [c.value for c in ws[1][:7]] == ["Pos", "Tier", "Player", "Base$", "Rec$", "Drafted", "Paid"]
    # Rec$ (column E) is a live formula reacting to Drafted/Paid.
    assert str(ws["E2"].value).startswith("=IF(F2")
    # Control block (column P / index 16) carries the remaining-pool math.
    labels = [ws.cell(row=r, column=16).value for r in range(1, 8)]
    assert "Remaining pool" in labels and "Remaining weight" in labels
