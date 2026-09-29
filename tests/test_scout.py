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


def test_check_prompt_carries_note_and_lines():
    from fantasy_football.scout import check_prompt

    p = check_prompt("DEN", 2, 2026, "Surtain erased the outside.", ["Parker Washington (WR) 12 tgt 7 rec 98 yds 0 TD"])
    assert "DEN defense from NFL week 2 (2026)" in p
    assert "NOTE:\nSurtain erased the outside." in p
    assert "- Parker Washington (WR) 12 tgt" in p
    assert "Return ONLY the note" in p
    assert "(none)" in check_prompt("DEN", 2, 2026, "x", [])


def test_pick_model_prefers_newest_ga_flash():
    from fantasy_football.scout import pick_model, resolve_model

    models = [
        {"name": "models/gemini-2.5-flash", "supportedGenerationMethods": ["generateContent"]},
        {"name": "models/gemini-3.0-flash", "supportedGenerationMethods": ["generateContent"]},
        {"name": "models/gemini-3.1-flash-preview", "supportedGenerationMethods": ["generateContent"]},
        {"name": "models/gemini-3.0-flash-lite", "supportedGenerationMethods": ["generateContent"]},
        {"name": "models/gemini-3.0-pro", "supportedGenerationMethods": ["generateContent"]},
        {"name": "models/gemini-3.5-flash", "supportedGenerationMethods": ["embedContent"]},
    ]
    assert pick_model(models) == "gemini-3.0-flash"
    assert pick_model([]) is None
    assert resolve_model("gemini-2.5-flash", "k") == "gemini-2.5-flash"  # explicit name passes through


def test_parse_sections_uncapped_and_check_prompt_next():
    from fantasy_football.scout import check_prompt, parse_sections

    names = {"DEN": ("Denver Broncos", "Denver"), "JAX": ("Jacksonville Jaguars", "Jacksonville")}
    text = "## Denver Broncos\n" + ("Long sentence here. " * 60) + "\n## Jacksonville Jaguars\nShort.\n"
    full = parse_sections(text, "DEN", "JAX", names, cap=False)
    assert len(full["DEN"]) > 900 and full["JAX"] == "Short."
    capped = parse_sections(text, "DEN", "JAX", names)
    assert len(capped["DEN"]) <= 900
    p = check_prompt("DEN", 2, 2026, "note", [], "vLA")
    assert "'Wk3 vLA:'" in p and "UNDER 850" in p


def test_lite_picker_and_cost_estimate():
    from fantasy_football.scout import PRICES, estimate_cost, pick_lite_model, pick_models

    models = [
        {"name": "models/gemini-3.8-flash", "supportedGenerationMethods": ["generateContent"]},
        {"name": "models/gemini-3.8-flash-lite", "supportedGenerationMethods": ["generateContent"]},
        {"name": "models/gemini-3.6-flash-lite", "supportedGenerationMethods": ["generateContent"]},
        {"name": "models/gemini-3.9-flash-lite-preview", "supportedGenerationMethods": ["generateContent"]},
    ]
    assert pick_lite_model(models) == "gemini-3.8-flash-lite"
    assert pick_models(models) == ["gemini-3.8-flash"]  # lite never becomes the main model
    assert pick_lite_model([]) is None
    # 16 grounded flash calls + 32 lite calls, typical token counts
    usage = {"flash_in": 400_000, "flash_out": 60_000, "lite_in": 100_000, "lite_out": 20_000,
             "grounded": 16}
    est = estimate_cost(usage)
    expect = (400_000 * 0.75 + 60_000 * 3.75 + 100_000 * 0.10 + 20_000 * 0.40) / 1e6 + 16 * 0.014
    assert abs(est - expect) < 1e-9 and est < 1.0
    assert PRICES["grounding_per_call"] == 0.014


def test_gemini_call_builds_generation_config(monkeypatch):
    import json

    from fantasy_football import scout

    captured = {}

    class _Resp:
        def __init__(self, body): self._b = json.dumps(body).encode()
        def read(self): return self._b
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_urlopen(req, timeout=0):
        captured["body"] = json.loads(req.data.decode())
        captured["url"] = req.full_url
        return _Resp({"candidates": [{"content": {"parts": [{"text": "note text"}]},
                                      "groundingMetadata": {"x": 1}}],
                      "usageMetadata": {"promptTokenCount": 1200, "candidatesTokenCount": 300,
                                        "thoughtsTokenCount": 150}})

    monkeypatch.setattr(scout.urllib.request, "urlopen", fake_urlopen)
    text, usage = scout.gemini_call("hi", "k", model="gemini-x", grounded=True,
                                    thinking_budget=1024, max_output=2200)
    assert text == "note text"
    assert usage == {"in": 1200, "out": 300, "thinking": 150, "grounded": 1}
    gen = captured["body"]["generationConfig"]
    assert gen["thinkingConfig"] == {"thinkingBudget": 1024} and gen["maxOutputTokens"] == 2200
    assert captured["body"]["tools"] == [{"google_search": {}}]
    assert "gemini-x:generateContent" in captured["url"]
    # plain call: no tools, thinking off, and grounded=0 when no grounding metadata
    def fake_plain(req, timeout=0):
        captured["body"] = json.loads(req.data.decode())
        return _Resp({"candidates": [{"content": {"parts": [{"text": "ok"}]}}],
                      "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 1}})
    monkeypatch.setattr(scout.urllib.request, "urlopen", fake_plain)
    text, usage = scout.gemini_call("hi", "k", model="gemini-x", grounded=False, thinking_budget=0)
    assert "tools" not in captured["body"] and captured["body"]["generationConfig"]["thinkingConfig"] == {"thinkingBudget": 0}
    assert usage["grounded"] == 0 and usage["thinking"] == 0
    assert scout.gemini_scout("hi", "k", model="gemini-x", grounded=False) == "ok"
