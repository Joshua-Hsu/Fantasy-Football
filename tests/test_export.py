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


def test_csv_readers_reject_malicious_input(tmp_path):
    from fantasy_football.cli import _read_fixed_prices, _read_manual_tiers

    p = tmp_path / "evil.csv"
    p.write_text(
        "key,manual_tier,price\n"
        "=cmd|' /C calc'!A1,1,5\n"   # formula key -> rejected
        "p100,99,5\n"               # tier clamped to 30
        "p101,notanint,5\n"         # bad tier -> skipped
        "d ABC,1,5\n"               # space in key -> rejected
        "p102,2,=HYPERLINK(1)\n"    # bad price -> skipped
    )
    tiers = _read_manual_tiers(str(p))
    prices = _read_fixed_prices(str(p))
    assert all(k.startswith(("p", "d")) and "=" not in k and " " not in k for k in tiers)
    assert tiers["p100"] == 30            # clamped
    assert "p101" not in tiers            # bad tier skipped
    assert tiers["p102"] == 2
    assert "p102" not in prices           # bad price skipped


def test_merge_ratings_averages_across_files(tmp_path):
    from fantasy_football.cli import _merge_ratings

    a = tmp_path / "a.csv"
    a.write_text("key,rating\nprb0,100\nprb1,50\n")
    b = tmp_path / "b.csv"
    b.write_text("key,rating\nprb0,200\n")  # prb1 absent here
    merged = _merge_ratings([str(a), str(b)], base={"prb1": 999.0, "prb5": 10.0})
    assert merged["prb0"] == 150.0       # (100 + 200) / 2
    assert merged["prb1"] == 50.0        # only one file had it -> that value (not base)
    assert merged["prb5"] == 10.0        # nobody picked -> base carried forward


def test_master_round_trip_rating_and_derived_tiers(session, tmp_path):
    _seed(session)
    from fantasy_football.cli import _read_ratings
    from fantasy_football.export import write_tiers_csv

    out = tmp_path / "master.csv"
    # Hand the best RB a boosted rating; the writer should derive tiers from it.
    ratings = {"prb0": 500.0, "prb1": 120.0, "prb2": 90.0}
    write_tiers_csv(session, str(out), ratings=ratings, prices={"prb0": 42.0}, year=2025,
                    config=LeagueConfig(teams=1))
    text = out.read_text()
    assert text.splitlines()[0].startswith("key,manual_tier,rating,name")
    back = _read_ratings(str(out))
    assert back["prb0"] == 500.0
    # Boosted prb0 lands in the top tier.
    import csv as _csv
    rows = {r["key"]: r for r in _csv.DictReader(out.open())}
    assert int(rows["prb0"]["manual_tier"]) == 1
    assert rows["prb0"]["price"] == "42.0"


def test_safe_cell_guards_formulas():
    from fantasy_football.export import _safe_cell

    assert _safe_cell("=1+1") == "'=1+1"
    assert _safe_cell("+danger") == "'+danger"
    assert _safe_cell("@x") == "'@x"
    assert _safe_cell("-2") == "'-2"
    assert _safe_cell("Ja'Marr Chase") == "Ja'Marr Chase"   # normal text untouched
    assert _safe_cell(123) == 123
