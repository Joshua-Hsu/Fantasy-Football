"""Offline tests for the weekly scouting loop (no network)."""

import csv

import pytest

from fantasy_football.db import create_db_engine, get_sessionmaker, init_db
from fantasy_football.scout import (append_notes, box_lines, cap_note, clean_text, game_pairs,
                                    noted_teams, parse_sections, scout_prompt)
from tests.test_matchups import _seed


@pytest.fixture()
def session():
    engine = create_db_engine(":memory:")
    init_db(engine)
    with get_sessionmaker(engine)() as s:
        yield s


def test_game_pairs_and_prompt(session):
    _seed(session)
    pairs = game_pairs(session, 2026, 1)
    assert [(p["a"], p["b"]) for p in pairs] == [("GB", "CHI"), ("DET", "MIN")]
    gb = pairs[0]
    assert gb["a_next"] == "bye" and gb["b_next"] == "vDET"  # wk2: CHI hosts DET, GB idle
    text = scout_prompt(session, 2026, 1, "GB", "CHI", a_next=gb["a_next"], b_next=gb["b_next"])
    assert "Packers and Bears defenses after their week 1 game" in text
    assert "Packers plays nobody (bye week) next week and Bears plays the Lions" in text
    # The verified box lines ride along so the model anchors on real numbers.
    assert "Against the Packers defense:" in text and "wrA (WR) 10 tgt 8 rec 140 yds 2 TD" in text
    gb_block = text.split("Against the Packers defense:")[1].split("Against the Bears defense:")[0]
    assert "wrA" in gb_block and "wrB" not in gb_block  # wrB played FOR GB, not against it


def test_box_lines_volume_filter(session):
    _seed(session)
    faced = box_lines(session, 2026, 1, "CHI")  # CHI's defense faced GB's offense
    assert any(x.startswith("wrB (WR) 4 tgt") for x in faced)
    assert not any(x.startswith("rbA") for x in faced)  # rushes without attempts: no volume
    assert box_lines(session, 2026, 1, "XXX") == []


def test_parse_sections_and_cap():
    names = {"DEN": ("Denver Broncos", "Denver"), "JAX": ("Jacksonville Jaguars", "Jacksonville")}
    text = (
        "## [Denver Broncos](https://x)\nNickel 75% [1, 2]. Surtain locked the boundary.\n"
        "Fantasy Translation:\nFunnel to the slot.\n"
        "## Jacksonville Jaguars\nBase 4-3. " + "Slot torched. " * 120 + "\n"
        "Would you like to review anything else?\n[1] https://a\n[2] https://b\n"
    )
    notes = parse_sections(text, "DEN", "JAX", names)
    assert notes["DEN"].startswith("Nickel 75%. Surtain locked the boundary.")
    assert "[1" not in notes["DEN"] and "https" not in notes["DEN"]
    assert "Fantasy Translation: Funnel to the slot." in notes["DEN"]
    assert len(notes["JAX"]) <= 900 and "Would you like" not in notes["JAX"]
    assert cap_note("a" * 950) == "a" * 900 + "..."
    assert clean_text("**bold** [link](http://x) [3]") == "bold link"


def test_append_and_noted(tmp_path):
    path = tmp_path / "notes.csv"
    path.write_text("team,date,note\n")
    n = append_notes(str(path), {"DEN": "x", "JAX": ""}, "2026-09-23")
    assert n == 1
    rows = list(csv.DictReader(open(path)))
    assert rows[-1]["team"] == "DEN" and rows[-1]["date"] == "2026-09-23"
    assert noted_teams(str(path), "2026-09-22") == {"DEN"}
    assert noted_teams(str(path), "2026-09-24") == set()


def test_prompt_template_file(tmp_path):
    from fantasy_football.scout import PROMPT_FILE, _PROMPT_FALLBACK, prompt_template

    # The committed file is the source of truth and carries every placeholder.
    text = prompt_template()
    for ph in ("{a}", "{b}", "{a_short}", "{b_short}", "{a_next}", "{b_next}", "{week}", "{prev}", "{year}"):
        assert ph in text
    assert PROMPT_FILE.endswith("prompts/defense_scout.txt")
    # Missing / empty file -> fallback, never a crash.
    assert prompt_template(str(tmp_path / "nope.txt")) == _PROMPT_FALLBACK
    (tmp_path / "empty.txt").write_text("")
    assert prompt_template(str(tmp_path / "empty.txt")) == _PROMPT_FALLBACK
