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


def test_personnel_flags_out_and_back():
    from fantasy_football.matchups import personnel_flags

    def wk(team, player, pos, week, pct):
        return {"team": team, "player": player, "position": pos,
                "week": week, "defense_pct": pct}

    rows = (
        # Core corner all year, then misses week 4 -> out.
        [wk("TB", "CB One", "CB", w, 0.95) for w in (1, 2, 3)] +
        [wk("TB", "CB One", "CB", 4, 0.0)] +
        # Core safety missed week 3, returns week 4 -> back.
        [wk("TB", "S Two", "S", w, 0.9) for w in (1, 2)] +
        [wk("TB", "S Two", "S", 3, 0.05), wk("TB", "S Two", "S", 4, 0.88)] +
        # Rotational guy (40%) disappearing is not a flag.
        [wk("TB", "LB Rot", "LB", w, 0.4) for w in (1, 2, 3)] +
        # Steady starter -> no flag.
        [wk("MIA", "LB Steady", "LB", w, 0.99) for w in (1, 2, 3, 4)]
    )
    flags = personnel_flags(rows, through_week=4)
    assert set(flags) == {"TB"}
    assert {(f["n"], f["w"]) for f in flags["TB"]} == {
        ("CB One", "out"), ("S Two", "back")}
    # Week 1 has no baseline: nothing is ever flagged.
    assert personnel_flags(rows, through_week=1) == {}


def test_team_pace_opponent_plays(session):
    from fantasy_football.matchups import team_pace
    from fantasy_football.models import TeamGameStats

    g1, g2, g3 = _seed(session)
    session.add_all([
        TeamGameStats(team_id=g1.home_team_id, game_id=g1.id, is_home=True, plays=75),
        TeamGameStats(team_id=g1.away_team_id, game_id=g1.id, is_home=False, plays=45),
        TeamGameStats(team_id=g2.home_team_id, game_id=g2.id, is_home=True, plays=60),
        TeamGameStats(team_id=g2.away_team_id, game_id=g2.id, is_home=False, plays=58),
    ])
    session.commit()
    pace = team_pace(session, 2026)
    # GB's opponent (CHI) ran 45 plays; CHI's opponent (GB) ran 75.
    assert pace["GB"]["opl"] == 45.0 and pace["GB"]["rk"] == 1
    assert pace["CHI"]["opl"] == 75.0 and pace["CHI"]["rk"] == 4


def test_read_defense_notes(tmp_path):
    from fantasy_football.matchups import read_defense_notes

    path = tmp_path / "notes.csv"
    path.write_text(
        "team,date,note\n"
        "TB,2026-09-15,Nickel-heavy; funnels to the middle\n"
        "tb,2026-09-01,Older note\n"
        "TOOLONG,2026-09-15,skipped - bad team code\n"
        ",2026-09-15,skipped - no team\n"
        "TB,2026-09-20," + "x" * 1000 + "\n"
    )
    notes = read_defense_notes(str(path))
    assert set(notes) == {"TB"}
    # Newest first, lowercase team normalized, long text capped.
    assert notes["TB"][0]["d"] == "2026-09-20"
    assert len(notes["TB"][0]["t"]) == 900
    assert notes["TB"][-1]["d"] == "2026-09-01"
    assert read_defense_notes(str(tmp_path / "missing.csv")) == {}


def test_package_rates_and_pressure_metrics():
    from fantasy_football.matchups import package_rates, pressure_metrics

    snaps = [
        # NE week 1: 5 DBs near full-time + 2 LBs -> nickel look.
        *[{"team": "NE", "week": 1, "position": "CB", "defense_pct": 1.0}
          for _ in range(3)],
        {"team": "NE", "week": 1, "position": "S", "defense_pct": 1.0},
        {"team": "NE", "week": 1, "position": "FS", "defense_pct": 0.9},
        {"team": "NE", "week": 1, "position": "LB", "defense_pct": 1.0},
        {"team": "NE", "week": 1, "position": "ILB", "defense_pct": 0.8},
        {"team": "NE", "week": 1, "position": "DE", "defense_pct": 1.0},  # ignored
        {"team": "NE", "week": 2, "position": "CB", "defense_pct": 1.0},  # beyond cutoff
    ]
    rates = package_rates(snaps, through_week=1)
    assert rates["NE"]["db"] == pytest.approx(4.9)
    assert rates["NE"]["lb"] == pytest.approx(1.8)

    pbp = (
        [{"defteam": "TB", "pass": 1, "sack": 1, "qb_hit": 0} for _ in range(2)] +
        [{"defteam": "TB", "pass": 1, "sack": 0, "qb_hit": 1} for _ in range(2)] +
        [{"defteam": "TB", "pass": 1, "sack": 0, "qb_hit": 0} for _ in range(6)] +
        [{"defteam": "TB", "pass": 0, "sack": 0, "qb_hit": 0},
         {"defteam": "NE", "pass": 1, "sack": 0, "qb_hit": 0}]
    )
    pm = pressure_metrics(pbp)
    assert pm["TB"]["prs"] == pytest.approx(40.0)
    assert pm["TB"]["rkPrs"] == 1
    assert pm["NE"]["prs"] == pytest.approx(0.0)


def test_implied_totals():
    from fantasy_football.matchups import implied_totals

    rows = [
        {"week": 2, "home_team": "SF", "away_team": "MIA",
         "spread_line": 13.5, "total_line": 44.5},
        {"week": 2, "home_team": "NE", "away_team": "PIT",
         "spread_line": 5.5, "total_line": 41.5},
        {"week": 2, "home_team": "XX", "away_team": "YY",
         "spread_line": float("nan"), "total_line": 47.0},   # no line yet
        {"week": 3, "home_team": "SF", "away_team": "LA",
         "spread_line": 3.0, "total_line": 50.0},            # other week
    ]
    v = implied_totals(rows, week=2)
    assert v["SF"] == {"it": 29.0, "gt": 44.5}
    assert v["MIA"] == {"it": 15.5, "gt": 44.5}
    assert v["NE"]["it"] == 23.5 and v["PIT"]["it"] == 18.0
    assert "XX" not in v and "LA" not in v


def test_vegas_weeks_history():
    from fantasy_football.matchups import vegas_weeks

    rows = [
        {"week": 1, "home_team": "GB", "away_team": "CHI", "spread_line": 3.0,
         "total_line": 44.0, "home_score": 30, "away_score": 10},
        {"week": 2, "home_team": "SF", "away_team": "MIA", "spread_line": 13.5,
         "total_line": 44.5, "home_score": float("nan"), "away_score": float("nan")},
        {"week": 3, "home_team": "X", "away_team": "Y", "spread_line": float("nan"),
         "total_line": float("nan")},   # no line posted yet
    ]
    w = vegas_weeks(rows)
    assert sorted(w) == [1, 2]
    g = w[1][0]
    assert (g["ith"], g["ita"], g["hs"], g["as"]) == (23.5, 20.5, 30, 10)
    u = w[2][0]
    assert u["hs"] is None and u["as"] is None and u["ith"] == 29.0
