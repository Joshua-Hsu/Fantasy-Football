"""Tests for the measured defensive scheme layer (defense_measured.py).

Fully offline: every source is a small in-memory fixture shaped like the
nflverse release rows (pbp, FTN charting, participation, snap counts,
contracts, official injury report).
"""

from __future__ import annotations

import csv
import json

import pytest

from fantasy_football.db import create_db_engine, get_sessionmaker, init_db
from fantasy_football.defense_measured import (
    SchemeSources,
    aggregate,
    formation,
    key_defenders,
    measured_rows,
    scheme_payload,
    scheme_plays,
    season_blocks,
    shift_flags,
    weekly_note,
    write_measured_notes,
)
from fantasy_football.matchups import read_defense_profiles, write_dvp_js

NICKEL = "3 CB, 2 DE, 2 DT, 1 FS, 1 MLB, 1 OLB, 1 SS"       # 4-2-5
BASE43 = "2 CB, 2 DE, 2 DT, 1 FS, 2 MLB, 1 OLB, 1 SS"       # 4-3-4
DIME = "4 CB, 2 DE, 2 DT, 1 FS, 1 MLB, 1 SS"                # 4-1-6


@pytest.mark.parametrize("personnel,label,db", [
    (NICKEL, "4-2-5 nickel", 5),
    (BASE43, "4-3-4 base", 4),
    ("2 CB, 2 DT, 1 FS, 2 ILB, 2 OLB, 1 SS, 1 NT", "3-4-4 base", 4),
    ("2 CB, 4 DT, 1 FS, 1 ILB, 2 OLB, 1 SS", "4-3-4 base", 4),
    ("3 CB, 2 DT, 1 FS, 2 ILB, 2 OLB, 1 SS", "2-4-5 nickel", 5),
    ("3 CB, 1 DE, 1 NT, 1 DT, 1 FS, 2 ILB, 1 OLB, 1 SS", "3-3-5 nickel", 5),
    (DIME, "4-1-6 dime", 6),
    ("4 CB, 1 DE, 1 DT, 1 NT, 2 FS, 1 OLB, 1 SS", "3-1-7 quarter", 7),
    ("2 CB, 3 DT, 2 DE, 1 SS, 3 MLB", "5-3-3 goal line", 3),
    ("2 CB, 2 DE, 1 DT, 1 NT, 1 FS, 3 ILB, 1 SS", "4-3-4 base", 4),
    ("2 CB, 2 DE, 3 DT, 1 FS, 2 MLB, 1 SS", "5-2-4 base", 4),
    ("2 CB, 2 DE, 2 DT, 2 MLB, 2 OLB, 1 SS", "4-4-3 base", 3),
    ("3 CB, 1 DE, 2 FS, 3 ILB, 1 OLB, 1 SS", "1-4-6", 6),     # 11, unnamed -> digits
    ("3 CB, 2 DE, 2 DT, 1 FS, 1 MLB, 1 SS", "4-1-5", 5),      # 10 on the field
    ("2 CB, 1 FB, 2 ILB, 1 K, 2 OLB, 1 RB, 1 SS, 1 TE", "0-4-3", 3),  # special teams
])
def test_formation_labels(personnel, label, db):
    assert formation(personnel) == (label, db)


@pytest.mark.parametrize("bad", [None, float("nan"), "", "garbage"])
def test_formation_unparseable(bad):
    assert formation(bad) is None


# ---- one fake game ------------------------------------------------------

GID = "2026_01_BAL_BUF"


def _game():
    """BAL on defense for 10 plays: 6 pass (incl. a sack), 4 run.

    Formations: 6x nickel, 3x base 4-3, 1x dime. Rushers on the 6 pass
    plays: 4,4,5,6,4,3 -> blitz 2/6 = 33%, mean 4.33. Box on all 10:
    6,6,7,8,6,7,7,5,6,8 -> 7+ = 5/10, light (<=6) = 5/10.
    """
    pbp, ftn, part = [], [], []
    forms = [NICKEL] * 6 + [BASE43] * 3 + [DIME]
    rush = [4, 4, 5, 6, 4, 3, 0, 0, 0, 0]
    box = [6, 6, 7, 8, 6, 7, 7, 5, 6, 8]
    for i in range(10):
        pid = 100 + i
        is_pass = i < 6
        pbp.append({"game_id": GID, "play_id": pid, "week": 1, "season_type": "REG",
                    "defteam": "BAL", "pass": 1 if is_pass else 0, "rush": 0 if is_pass else 1})
        ftn.append({"nflverse_game_id": GID, "nflverse_play_id": pid,
                    "n_pass_rushers": rush[i], "n_defense_box": box[i],
                    "n_blitzers": max(rush[i] - 4, 0)})
        part.append({"nflverse_game_id": GID, "play_id": pid, "defense_personnel": forms[i]})
    # A punt (neither pass nor run) and an offense play for BUF are ignored.
    pbp.append({"game_id": GID, "play_id": 999, "week": 1, "season_type": "REG",
                "defteam": "BAL", "pass": 0, "rush": 0})
    return pbp, ftn, part


def test_one_game_measured_block():
    pbp, ftn, part = _game()
    blk = aggregate(scheme_plays(pbp, ftn, part))
    assert blk["src"] == "participation"
    assert blk["base"] == ["4-2-5 nickel", 60]
    assert blk["second"] == ["4-3-4 base", 30]
    assert blk["db5"] == 70
    assert blk["blitz"] == 33
    assert blk["rushers"] == 4.3
    assert blk["box7"] == 50
    assert blk["light"] == 50
    # n_blitzers is a cross-check only: every 5+ rush here has a blitzer.
    pass_rows = ftn[:6]
    assert sum(1 for r in pass_rows if r["n_blitzers"] > 0) == \
        sum(1 for r in pass_rows if r["n_pass_rushers"] >= 5)


def test_missing_participation_degrades_to_ftn_only():
    pbp, ftn, _ = _game()
    blk = aggregate(scheme_plays(pbp, ftn, None))
    assert blk["src"] == "ftn-only"
    assert blk["base"] is None and blk["second"] is None and blk["db5"] is None
    assert blk["blitz"] == 33 and blk["box7"] == 50
    note = weekly_note(1, blk, [{"n": "Roquan Smith", "pos": "LB", "snap": 100,
                                 "star": "$", "out": False}])
    assert note.startswith("Wk1 measured: base/2nd: n/a (no participation file)")
    assert "5+ DB n/a" in note
    assert "7+ box 50%" in note and "light box (<=6) 50%" in note
    assert "blitz 33% (avg 4.3 rushers)" in note
    assert "key: Roquan Smith LB 100% $" in note


def test_participation_note_format():
    pbp, ftn, part = _game()
    blk = aggregate(scheme_plays(pbp, ftn, part))
    note = weekly_note(1, blk, [])
    assert note == ("Wk1 measured: base 4-2-5 nickel 60% · 2nd 4-3-4 base 30% · 5+ DB 70% · "
                    "blitz 33% (avg 4.3 rushers, 7+ box 50%) · key: n/a")


def test_uncharted_plays_are_dropped():
    pbp, ftn, part = _game()
    pbp.append({"game_id": "2026_02_BAL_CLE", "play_id": 1, "week": 2, "season_type": "REG",
                "defteam": "BAL", "pass": 1, "rush": 0})
    plays = scheme_plays(pbp, ftn, part)
    assert {p["wk"] for p in plays} == {1}
    assert season_blocks(plays)["BAL"]["wk"] == 1


# ---- key defenders ------------------------------------------------------

def _snaps(team, rows):
    out = []
    for name, pos, pcts in rows:
        for wk, pct in enumerate(pcts, start=1):
            out.append({"week": wk, "player": name, "position": pos, "team": team,
                        "defense_pct": pct})
    return out


CONTRACTS = [
    {"player": "Rich Corner", "is_active": True, "apy": 20.0, "draft_year": 2019,
     "draft_round": 3, "draft_overall": 80},
    {"player": "Cheap Corner", "is_active": True, "apy": 1.0, "draft_year": 2023,
     "draft_round": 5, "draft_overall": 150},
    {"player": "Rookie Edge", "is_active": True, "apy": 9.0, "draft_year": 2026,
     "draft_round": 1, "draft_overall": 14},
    {"player": "Old Star", "is_active": True, "apy": 5.0, "draft_year": 2014,
     "draft_round": 1, "draft_overall": 6},
    {"player": "Rich Corner", "is_active": False, "apy": 30.0},   # inactive: ignored
    {"player": "Mike Backer Jr.", "is_active": True, "apy": 2.0, "draft_year": 2022,
     "draft_round": 2, "draft_overall": 40},
]


def test_key_defenders_star_bonus_and_tags():
    snaps = _snaps("BAL", [
        ("Rich Corner", "CB", [0.8, 0.8]),
        ("Cheap Corner", "CB", [0.85, 0.85]),
        ("Rookie Edge", "DE", [0.7, 0.7]),
        ("Old Star", "DT", [0.55, 0.55]),
        ("Mike Backer", "LB", [0.95, 0.95]),     # normalized match to "Jr."
        ("Nobody Known", "S", [0.9, 0.9]),       # no contract row
        ("Part Timer", "LB", [0.4, 0.4]),        # below 50%
        ("One Game", "CB", [0.99, 0.0]),          # played one of two games
    ])
    unmatched: set = set()
    key = key_defenders(snaps, through_week=2, season=2026, contracts=CONTRACTS,
                        unmatched=unmatched)["BAL"]
    names = [k["n"] for k in key]
    assert names.index("Rich Corner") < names.index("Cheap Corner")
    by = {k["n"]: k for k in key}
    assert by["Rich Corner"]["star"] == "$" and by["Rich Corner"]["snap"] == 80
    assert by["Rookie Edge"]["star"] == "R1"
    assert by["Old Star"]["star"] == "R1"           # top-10 pick, any year
    assert by["Mike Backer"]["star"] == ""          # matched, not a star
    assert "Part Timer" not in by and "One Game" not in by
    assert len(key) == 6
    assert unmatched == {"BAL Nobody Known"}
    assert {k["pos"] for k in key} >= {"CB", "LB", "DE"}


def test_key_defenders_keep_one_per_group():
    # Six DBs outrank the lone linebacker and lineman, who still make the list.
    rows = [(f"DB {i}", "CB", [0.99, 0.99]) for i in range(7)]
    rows += [("Lone LB", "LB", [0.55, 0.55]), ("Lone DL", "DT", [0.52, 0.52])]
    key = key_defenders(_snaps("BUF", rows), through_week=2, season=2026)["BUF"]
    assert len(key) == 6
    assert {"Lone LB", "Lone DL"} <= {k["n"] for k in key}


def test_key_defender_out_tag_and_note():
    snaps = _snaps("BAL", [("Rich Corner", "CB", [0.9, 0.9])])
    official = [{"n": "Rich Corner", "tm": "BAL", "pos": "CB", "st": "OUT"},
                {"n": "Rich Corner", "tm": "KC", "pos": "CB", "st": "OUT"}]
    key = key_defenders(snaps, through_week=2, season=2026, contracts=CONTRACTS,
                        official=official)["BAL"]
    assert key[0]["out"] is True
    assert "Rich Corner CB 90% $ OUT" in weekly_note(2, {"src": "ftn-only"}, key)


def test_key_defenders_single_game_team():
    snaps = _snaps("BAL", [("Week One", "S", [0.9])])
    assert key_defenders(snaps, through_week=1, season=2026)["BAL"][0]["n"] == "Week One"


def test_unmatched_contracts_do_not_crash():
    snaps = _snaps("BAL", [("Sauce Gardner", "CB", [0.9, 0.9])])
    junk = [{"player": None, "is_active": True}, {"player": "X", "is_active": True,
                                                  "apy": float("nan")}]
    key = key_defenders(snaps, through_week=2, season=2026, contracts=junk)
    assert key["BAL"][0]["star"] == ""


# ---- notes file ---------------------------------------------------------

def test_measured_notes_idempotent_and_force(tmp_path):
    path = tmp_path / "defense_notes.csv"
    path.write_text('team,date,note\nBAL,2026-09-15,"hand paste, kept"\n')
    n, skipped = write_measured_notes(str(path), {"BAL": "Wk3 measured: a", "BUF": "Wk3 measured: b"},
                                      week=3, date="2026-09-23")
    assert (n, skipped) == (2, [])
    n, skipped = write_measured_notes(str(path), {"BAL": "Wk3 measured: c"}, week=3, date="2026-09-24")
    assert (n, skipped) == (0, ["BAL"])
    n, skipped = write_measured_notes(str(path), {"BAL": "Wk3 measured: c"}, week=3,
                                      date="2026-09-24", force=True)
    assert (n, skipped) == (1, [])
    rows = list(csv.DictReader(open(path, newline="")))
    assert [r["note"] for r in rows if r["team"] == "BAL"] == ["hand paste, kept", "Wk3 measured: c"]
    assert measured_rows(str(path)) == {("BAL", 3), ("BUF", 3)}
    # A new week is a new row, not a refresh.
    n, _ = write_measured_notes(str(path), {"BAL": "Wk4 measured: d"}, week=4, date="2026-09-30")
    assert n == 1


def test_read_defense_profiles_newest_wins(tmp_path):
    path = tmp_path / "defense_profiles.csv"
    path.write_text(
        "team,date,profile\n"
        'BAL,2026-10-26,"seed text"\n'
        'BAL,2026-11-10,"adjusted after a shift"\n'
        'BAL,2026-10-01,"older, appended out of order"\n'
        'TOOLONG,2026-11-10,"bad team code"\n'
        "BUF,2026-10-26,\n"
        f'BUF,2026-10-26,"{"x" * 2000}"\n')
    prof = read_defense_profiles(str(path))
    assert prof["BAL"] == {"d": "2026-11-10", "t": "adjusted after a shift"}
    assert len(prof["BUF"]["t"]) == 900
    assert set(prof) == {"BAL", "BUF"}
    assert read_defense_profiles(str(tmp_path / "missing.csv")) == {}


def test_seed_profiles_cover_all_32():
    prof = read_defense_profiles("defense_profiles.csv")
    assert len(prof) == 32


# ---- shift detection ----------------------------------------------------

def _weeks(team, blitz_by_week):
    """FTN-only plays: 10 pass plays a week, ``k`` of them with 5 rushers."""
    pbp, ftn = [], []
    for wk, k in blitz_by_week.items():
        gid = f"2026_{wk:02d}_{team}_X"
        for i in range(10):
            pbp.append({"game_id": gid, "play_id": i, "week": wk, "season_type": "REG",
                        "defteam": team, "pass": 1, "rush": 0})
            ftn.append({"nflverse_game_id": gid, "nflverse_play_id": i,
                        "n_pass_rushers": 5 if i < k else 4, "n_defense_box": 6})
    return scheme_plays(pbp, ftn)


def test_shift_two_weeks_flags():
    plays = _weeks("NYG", {1: 2, 2: 2, 3: 2, 4: 2, 5: 4, 6: 5})
    flags = shift_flags(plays)
    assert flags["NYG"]["since"] == 5
    assert flags["NYG"]["what"] == "blitz 20 -> 45"


def test_shift_one_week_no_flag():
    assert shift_flags(_weeks("NYG", {1: 2, 2: 2, 3: 2, 4: 2, 5: 2, 6: 6})) == {}
    # Opposite directions in the two weeks are noise, not a shift.
    assert shift_flags(_weeks("NYG", {1: 4, 2: 4, 3: 4, 4: 4, 5: 7, 6: 1})) == {}


def test_shift_base_formation_change():
    pbp, ftn, part = [], [], []
    for wk in range(1, 6):
        form = NICKEL if wk <= 3 else BASE43
        gid = f"2026_{wk:02d}_BAL_X"
        for i in range(10):
            pbp.append({"game_id": gid, "play_id": i, "week": wk, "defteam": "BAL",
                        "pass": 0, "rush": 1})
            part.append({"nflverse_game_id": gid, "play_id": i, "defense_personnel": form})
    flags = shift_flags(scheme_plays(pbp, None, part))
    assert flags["BAL"]["since"] == 4
    assert "base 4-2-5 nickel -> 4-3-4 base" in flags["BAL"]["what"]
    assert "5+ DB 100 -> 0" in flags["BAL"]["what"]


# ---- payload ------------------------------------------------------------

def test_scheme_payload_and_dvp_js(tmp_path):
    pbp, ftn, _ = _game()
    src = SchemeSources(year=2026, pbp=pbp, ftn=ftn,
                        snaps=_snaps("BAL", [("Rich Corner", "CB", [0.9])]),
                        contracts=CONTRACTS)
    prof = {"BAL": {"d": "2026-10-26", "t": "seed"}, "KC": {"d": "2026-10-26", "t": "no data"}}
    scheme = scheme_payload(src, prof)
    assert scheme["BAL"]["d"] == "2026-10-26"
    m = scheme["BAL"]["m"]
    assert m["src"] == "ftn-only" and m["blitz"] == 33 and m["last3"]["blitz"] == 33
    assert m["key"][0] == {"n": "Rich Corner", "pos": "CB", "snap": 90, "star": "$", "out": False}
    assert "m" not in scheme["KC"] and scheme["KC"]["t"] == "no data"

    engine = create_db_engine(":memory:")
    init_db(engine)
    with get_sessionmaker(engine)() as session:
        out = tmp_path / "dvp.js"
        write_dvp_js(session, str(out), 2026, scheme_sources=src, defense_profiles_text=prof)
        text = out.read_text()
        payload = json.loads(text[text.index("=") + 1:].rstrip().rstrip(";"))
        assert payload["scheme"]["BAL"]["m"]["src"] == "ftn-only"
        # No scheme inputs -> no key at all (old pages keep working).
        write_dvp_js(session, str(out), 2026)
        assert "scheme" not in out.read_text()
