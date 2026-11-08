"""Tests for the lineup command (lineup.py + `cli lineup`). Fully offline."""

from __future__ import annotations

import datetime as dt
import json

import pytest

from fantasy_football import cli
from fantasy_football.db import create_db_engine, get_sessionmaker, init_db
from fantasy_football.lineup import (
    LineupError,
    Model,
    build_lineup,
    load_payload,
    model_ppg,
    norm_name,
    render_markdown,
    resolve_roster,
)
from fantasy_football.models import Game, Player, PlayerGameStats, Season, Team, TeamGameStats


@pytest.fixture()
def session():
    engine = create_db_engine(":memory:")
    init_db(engine)
    with get_sessionmaker(engine)() as s:
        yield s


def _payload(**over) -> dict:
    """A two-defense sidecar small enough to compute by hand."""
    p = {
        "year": 2026, "through_week": 6, "week": 7, "positions": ["QB", "RB", "WR", "TE"],
        "teams": {
            "A": {"QB": 18.0, "RB": 20.0, "WR": 25.0, "TE": 8.0, "pace": {"opl": 66.0, "rk": 30}},
            "B": {"QB": 18.0, "RB": 30.0, "WR": 25.0, "TE": 12.0, "pace": {"opl": 60.0, "rk": 3}},
        },
        "baseline": {"A": {"RB": 10.0}, "B": {"RB": 30.0}},
        "vegas": {"X": {"it": 27.0, "gt": 48.0}, "A": {"it": 21.0, "gt": 48.0}},
        "opp": {"X": "vA", "A": "@X", "Y": "vB", "B": "@Y"},
        "profiles": {"A": {"rk": {"RB": 20, "WR": 12}, "rk3": {"RB": 28, "WR": 9},
                           "trend": {"RB": 8, "WR": -3}}},
        "official": [],
        "flags": {},
    }
    p.update(over)
    return p


def _row(name, pos, team, ppg, games=6, last3=None, status="ACT"):
    last3 = last3 if last3 is not None else [{"wk": w, "tgt": 5, "car": 5, "pts": ppg} for w in (4, 5, 6)]
    return {"name": name, "pos": pos, "team": team, "roster_team": team, "roster_status": status,
            "games": games, "season_ppg": ppg, "last3": last3}


def test_model_matches_hand_computed_app_case():
    m = Model(_payload())
    # wNow = min(6/6, .9) = .9; DvP A vs RB = .9*20 + .1*10 = 19, B = 30 -> avg 24.5.
    assert m.w_now == pytest.approx(0.9)
    assert m.dvp_blend("A", "RB") == pytest.approx(19.0)
    assert m.dvp_avg["RB"] == pytest.approx(24.5)
    # Vegas avg (27+21)/2 = 24; pace (66+60)/2 = 63 (current-season from week 4).
    # 10 x (27/24)^.45 x (19/24.5)^.35 x (66/63)^.2 = 9.7368 (docs/app.js modelProj).
    assert m.proj(10.0, "RB", "X", "A") == pytest.approx(9.7368, abs=0.01)
    # K: own implied only. 8 x 1.125^.8
    assert m.proj(8.0, "K", "X", "A") == pytest.approx(8.7905, abs=0.01)
    # DST: opponent's implied inverted. 6 x (24/27)^.8
    assert m.proj(6.0, "DST", "A", "X") == pytest.approx(5.4605, abs=0.01)
    # No ppg -> no projection (the app's `if (!e.ppg) return null`).
    assert m.proj(None, "RB", "X", "A") is None


def test_early_season_reads_baseline_pace_and_recent_form_blend():
    m = Model(_payload(through_week=2, baseline={"A": {"RB": 10.0, "pace": {"opl": 50.0}}}))
    assert m.pace_of("A") == {"opl": 50.0}  # baseline pace before week 4
    assert m.w_now == pytest.approx(2 / 6)
    last3 = [{"pts": 20.0}, {"pts": 20.0}, {"pts": 20.0}]
    assert model_ppg(10.0, last3, 6) == pytest.approx(15.0)  # 50/50 season vs last 3
    assert model_ppg(10.0, last3[:2], 2) == pytest.approx(10.0)  # <3 games: season only


def test_out_ir_and_bye_are_unavailable():
    payload = _payload(official=[{"n": "Hurt Guy", "tm": "X", "pos": "WR", "st": "OUT",
                                  "inj": "Ribs", "prac": "DNP"}])
    rows = [_row("Hurt Guy", "WR", "X", 20), _row("Bye Guy", "WR", "Z", 18),
            _row("Ir Guy", "RB", "X", 18, status="RES"), _row("Fine Guy", "WR", "X", 5)]
    res = build_lineup(payload, rows)
    out = {u["player"]: u["reason"] for u in res["unavailable"]}
    assert out == {"Hurt Guy": "OUT (Ribs)", "Bye Guy": "bye", "Ir Guy": "IR (roster status RES)"}
    starters = {s["slot"]: (s["starter"] or {}).get("name") for s in res["slots"]}
    assert starters["WR1"] == "Fine Guy"
    assert "Hurt Guy" not in starters.values()


def test_flex_takes_best_remaining_rb_wr_te_and_alternatives_are_ranked():
    rows = [_row("R1", "RB", "X", 20), _row("R2", "RB", "X", 18), _row("R3", "RB", "X", 15),
            _row("W1", "WR", "X", 19), _row("W2", "WR", "X", 17), _row("W3", "WR", "X", 10),
            _row("T1", "TE", "X", 9), _row("T2", "TE", "X", 8)]
    res = build_lineup(_payload(), rows)
    slots = {s["slot"]: s for s in res["slots"]}
    assert slots["FLEX"]["starter"]["name"] == "R3"
    assert [a["name"] for a in slots["FLEX"]["alternatives"]] == ["W3", "T2"]
    assert [a["name"] for a in slots["RB1"]["alternatives"]] == []
    assert slots["TE"]["starter"]["name"] == "T1"
    assert slots["K"]["starter"] is None
    md = render_markdown(res)
    assert "## FLEX" in md and "R3 (RB, X)" in md


def test_questionable_starter_flagged_with_next_man_up():
    payload = _payload(official=[{"n": "Q Back", "tm": "X", "pos": "RB", "st": "QUESTIONABLE",
                                  "inj": "Ankle", "prac": "LP"}])
    rows = [_row("Q Back", "RB", "X", 20), _row("R2", "RB", "X", 18),
            _row("W1", "WR", "X", 19), _row("W2", "WR", "X", 17), _row("Spare", "WR", "X", 12),
            _row("R3", "RB", "X", 11)]
    res = build_lineup(payload, rows)
    rb1 = res["slots"][1]["starter"]
    assert rb1["name"] == "Q Back" and rb1["flag"] == "QUESTIONABLE (Ankle)"
    # Re-filling without him, the one player who enters the lineup is R3.
    assert rb1["next_man_up"] == "R3"
    assert res["watch"] == [{"slot": "RB1", "player": "Q Back", "status": "QUESTIONABLE",
                             "next_man_up": "R3"}]
    assert "Q Back (QUESTIONABLE, RB1) -> R3" in render_markdown(res)


def _seed(session):
    x = Team(abbreviation="X", name="Xs")
    a = Team(abbreviation="A", name="As")
    session.add_all([x, a, Season(year=2026)])
    session.flush()
    games = []
    for wk in range(1, 6):
        home, away = (x, a) if wk % 2 else (a, x)
        games.append(Game(season_year=2026, week=wk, season_type="regular",
                          game_date=dt.date(2026, 9, 6) + dt.timedelta(days=7 * wk),
                          home_team_id=home.id, away_team_id=away.id, home_score=20, away_score=17))
    # Week 7 already final (a Sunday-afternoon run) - must not feed the projection.
    games.append(Game(season_year=2026, week=7, season_type="regular", game_date=dt.date(2026, 10, 25),
                      home_team_id=x.id, away_team_id=a.id, home_score=30, away_score=3))
    session.add_all(games)
    session.flush()
    p = Player(full_name="Kenneth Walker III", position="RB", slug="kw3", current_team="X",
               status="ACT", active=1)
    session.add(p)
    session.flush()
    for g in games:
        yards = 300 if g.week == 7 else 10 * g.week
        session.add(PlayerGameStats(player_id=p.id, game_id=g.id, team_id=x.id,
                                    rush_attempts=g.week, rush_yards=yards, targets=2))
        session.add(TeamGameStats(team_id=x.id, game_id=g.id, is_home=g.home_team_id == x.id,
                                  points_allowed=17, sacks=2))
    session.commit()


def test_resolve_roster_usage_and_unmatched(session):
    _seed(session)
    roster = [{"player": "kenneth walker", "pos": "RB", "team": "X"},
              {"player": "X", "pos": "DST", "team": "X"},
              {"player": "Nobody Atall", "pos": "WR", "team": "X"}]
    rows, unmatched = resolve_roster(session, roster, 2026, week=7)
    assert unmatched == ["Nobody Atall"]
    kw = rows[0]
    assert kw["name"] == "Kenneth Walker III"
    assert kw["games"] == 5  # the final week-7 game is excluded
    assert [g["wk"] for g in kw["last3"]] == [3, 4, 5]
    assert kw["last3"][-1] == {"wk": 5, "tgt": 2, "car": 5, "pts": 5.0}
    assert kw["season_ppg"] == pytest.approx(3.0)
    assert kw["locked"] is True
    assert rows[1]["pos"] == "DST" and rows[1]["games"] == 5
    assert norm_name("Kenneth Walker III") == norm_name("kenneth  walker")


def test_cli_lineup_end_to_end(tmp_path, capsys):
    db = tmp_path / "ff.db"
    engine = create_db_engine(db)
    init_db(engine)
    with get_sessionmaker(engine)() as s:
        _seed(s)
    dvp = tmp_path / "dvp.js"
    dvp.write_text("window.FF_DVP = " + json.dumps(_payload()) + ";\n")
    roster = tmp_path / "roster.csv"
    roster.write_text("player,pos,team,slot\nKenneth Walker III,RB,X,RB1\nX,DST,X,DST\n")
    args = ["--db", str(db), "lineup", "--roster", str(roster), "--dvp", str(dvp), "--injuries", ""]
    assert cli.main(args) == 0
    out = capsys.readouterr().out
    assert "## RB1" in out and "Kenneth Walker III (RB, X)" in out and "locked" in out

    assert cli.main(args + ["--json", "--week", "8"]) == 0
    res = json.loads(capsys.readouterr().out)
    assert res["week"] == 8 and res["slots"][1]["starter"]["name"] == "Kenneth Walker III"

    roster.write_text("player,pos,team,slot\nGhost Player,WR,X,WR1\n")
    assert cli.main(args) == 1
    assert "Ghost Player" in capsys.readouterr().err


def test_missing_or_stale_sidecar(tmp_path):
    with pytest.raises(LineupError, match="not found"):
        load_payload(str(tmp_path / "nope.js"))
    old = tmp_path / "old.js"
    old.write_text('window.FF_DVP = {"year": 2026, "through_week": 3, "teams": {}};')
    with pytest.raises(LineupError, match="predates.*opp"):
        load_payload(str(old))
