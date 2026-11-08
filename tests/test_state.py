"""Tests for the state-file schema, validator and editors (state.py).

Offline: the committed CSVs are read from the repo root, edits run on tmp
copies, and name resolution uses a seeded in-memory DB.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import shutil
from pathlib import Path

import pytest

from fantasy_football import state
from fantasy_football.cli import main
from fantasy_football.db import create_db_engine, get_sessionmaker, init_db
from fantasy_football.models import Game, Player, Season, Team

REPO = Path(__file__).resolve().parents[1]
STATE_FILES = ["roster.csv", "decisions.csv", "injuries.csv", "defense_profiles.csv"]


@pytest.fixture()
def session():
    engine = create_db_engine(":memory:")
    init_db(engine)
    with get_sessionmaker(engine)() as s:
        yield s


def _seed(session):
    for code in ("NE", "KC", "BUF", "ARI", "LA"):
        session.add(Team(abbreviation=code, name=code))
    session.add(Season(year=2026))
    session.flush()
    ne, kc = (session.query(Team).filter_by(abbreviation=c).one() for c in ("NE", "KC"))
    session.add_all([
        Game(season_year=2026, week=9, season_type="regular", home_team_id=ne.id,
             away_team_id=kc.id, home_score=20, away_score=17),
        Game(season_year=2026, week=10, season_type="regular", home_team_id=kc.id,
             away_team_id=ne.id),
    ])
    people = [
        ("Drake Maye", "QB", "NE", 1),
        ("D.J. Moore", "WR", "BUF", 1),
        ("Marvin Harrison Jr.", "WR", "ARI", 1),
        ("De'Von Achane", "RB", "MIA", 1),
        ("Emmett Johnson", "RB", "KC", 1),
        # Name ties: two active Mike Williams (team breaks it), and a
        # retired Josh Allen namesake (active narrowing picks the QB).
        ("Mike Williams", "WR", "NYJ", 1),
        ("Mike Williams", "WR", "PIT", 1),
        ("Josh Allen", "QB", "BUF", 1),
        ("Josh Allen", "LB", "JAX", 0),
    ]
    for name, pos, team, active in people:
        session.add(Player(full_name=name, position=pos, current_team=team, active=active,
                           slug=f"{name}-{team}"))
    session.commit()


def _copy(tmp_path: Path, *names: str) -> None:
    for n in names:
        shutil.copy(REPO / n, tmp_path / n)


ROSTER = "player,pos,team,slot\nDrake Maye,QB,NE,QB\nLA,DST,LA,DST\n" \
         'D.J. Moore,WR,BUF,"bench (ankle, expected to play)"\n'
DECISIONS = ("date,week,decision,who,board,override,rationale,outcome,verdict\n"
             '2026-11-01,9,Start Maye,claude,"Proj 20.1, green",no,why,,pending\n'
             "2026-11-01,9,Sit Moore,user,red,YES (structural),why,4.0,hit\n")


def _write(tmp_path: Path, roster: str = ROSTER, decisions: str = DECISIONS) -> None:
    (tmp_path / "roster.csv").write_text(roster, newline="")
    (tmp_path / "decisions.csv").write_text(decisions, newline="")


# --- byte-faithful round trips ---------------------------------------------

@pytest.mark.parametrize("name", STATE_FILES)
def test_real_file_round_trips_with_zero_diff(name):
    raw = (REPO / name).read_bytes()
    doc = state.CsvDoc.load(str(REPO / name))
    assert doc.text().encode("utf-8") == raw


@pytest.mark.parametrize("name", ["roster.csv", "decisions.csv"])
def test_real_file_round_trips_through_plain_csv_writer(name):
    """decisions.csv and roster.csv are already in the canonical shape
    (QUOTE_MINIMAL, \\n) the editors write. injuries.csv (legacy rows quote
    the note column needlessly) and defense_profiles.csv (CRLF) are not,
    which is why CsvDoc re-emits untouched rows verbatim."""
    raw = open(REPO / name, encoding="utf-8", newline="").read()
    buf = io.StringIO()
    csv.writer(buf, lineterminator="\n", quoting=csv.QUOTE_MINIMAL).writerows(
        csv.reader(io.StringIO(raw, newline="")))
    assert buf.getvalue() == raw


def test_edit_touches_only_the_edited_row():
    text = ('team,date,profile\r\nARI,2026-10-26,"quoted, kept"\r\n'
            'ATL,2026-10-26,"needless quotes"\r\nBAL,2026-10-26,plain\r\n')
    doc = state.CsvDoc.parse(text)
    assert doc.eol == "\r\n"
    doc.set_fields(doc.rows()[2], ["BAL", "2026-11-01", "new, text"])
    doc.append(["BUF", "2026-11-01", 'say "hi"'])
    assert doc.text() == ('team,date,profile\r\nARI,2026-10-26,"quoted, kept"\r\n'
                          'ATL,2026-10-26,"needless quotes"\r\n'
                          'BAL,2026-11-01,"new, text"\r\nBUF,2026-11-01,"say ""hi"""\r\n')


def test_multiline_field_and_missing_final_newline():
    text = 'a,b\n1,"two\nlines"\n3,4'
    doc = state.CsvDoc.parse(text)
    assert [r.fields for r in doc.rows()] == [["1", "two\nlines"], ["3", "4"]]
    assert [r.line for r in doc.rows()] == [2, 4]
    assert doc.text() == text
    doc.append(["5", "6"])
    assert doc.text() == text + "\n5,6\n"


# --- validation ------------------------------------------------------------

def test_committed_state_files_pass(session):
    """The dvp Action runs this before the build; a broken row must fail it."""
    probs = state.check_state(str(REPO), session)
    assert [str(p) for p in probs if p.level == "error"] == []


def test_cli_check_state_on_committed_files(capsys):
    assert main(["--db", ":memory:", "check-state", "--root", str(REPO)]) == 0
    assert "check-state: OK" in capsys.readouterr().out


def test_decision_rules(tmp_path):
    bad = ("date,week,decision,who,board,override,rationale,outcome,verdict\n"
           "2026-11-01,9,ok,claude,b,no,r,,pending\n"
           "11/01/2026,pre,x,gemini,b,conditional,r,o,mixed\n"
           "2026-11-01,9,ten,claude,b,no,r,o,hit,extra\n")
    _write(tmp_path, decisions=bad)
    probs = state.check_decisions(str(tmp_path / "decisions.csv"))
    msgs = [(p.line, p.message.split(" ")[0]) for p in probs]
    assert (3, "date") in msgs and (3, "week") in msgs and (3, "who") in msgs
    assert (3, "verdict") in msgs and (3, "override") in msgs
    assert any(p.line == 4 and "10 columns" in p.message for p in probs)
    assert not any(p.line == 2 for p in probs)


def test_injuries_and_profiles_rules(tmp_path):
    (tmp_path / "injuries.csv").write_text(
        "date,player,team,pos,status,injury,timeline,replacement,note\n"
        "2026-11-01,A,KC,WR,OUT,ankle (2-32, bad),x,,\n"
        "Nov 1,B,KC,WR,OUT,ankle,x,,\n", newline="")
    probs = state.check_injuries(str(tmp_path / "injuries.csv"))
    assert [(p.line, "columns" in p.message, "date" in p.message) for p in probs] == \
        [(2, True, False), (3, False, True)]
    (tmp_path / "defense_profiles.csv").write_text(
        "team,date,profile\nKC,2026-10-26,fine\nKCC,2026-10-26,x\n", newline="")
    probs = state.check_profiles(str(tmp_path / "defense_profiles.csv"))
    assert [p.line for p in probs] == [3]


def test_roster_rules_and_unmatched_names(tmp_path, session):
    _seed(session)
    _write(tmp_path, roster=ROSTER + "Nobody Real,RB,NE,bench\nMaye,QB,XX,bench\n"
                                     "Josh Allen,LB,BUF,bench\n")
    probs = state.check_roster(str(tmp_path / "roster.csv"), session)
    errs = [p for p in probs if p.level == "error"]
    assert any("no DB player matches 'Nobody Real'" in p.message for p in errs)
    assert any("'XX' is not a known team code" in p.message for p in errs)
    assert any(p.message.startswith("unmatched names: Nobody Real, Maye") for p in errs)
    warns = [p.message for p in probs if p.level == "warn"]
    assert any("DB position is QB, roster says LB" in w for w in warns)
    assert any("cannot field 9 starters" in w for w in warns)


def test_roster_warns_over_fifteen_active(tmp_path):
    rows = "".join(f"Back {chr(65 + i)},RB,NE,bench\n" for i in range(16)) + "Q,QB,NE,IR\n"
    _write(tmp_path, roster="player,pos,team,slot\n" + rows)
    probs = state.check_roster(str(tmp_path / "roster.csv"))
    assert any("16 non-IR players" in p.message and p.level == "warn" for p in probs)
    assert not [p for p in probs if p.level == "error"]


# --- name resolution -------------------------------------------------------

def test_resolve_normalises_suffix_and_punctuation(session):
    _seed(session)
    assert state.resolve_name(session, "DJ Moore").name == "D.J. Moore"
    assert state.resolve_name(session, "marvin harrison").name == "Marvin Harrison Jr."
    assert state.resolve_name(session, "Devon Achane").pos == "RB"
    assert state.resolve_name(session, "Josh Allen").pos == "QB"  # active wins
    assert state.resolve_name(session, "la") == state.Resolved("LA", "DST", "LA")


def test_resolve_ambiguity_and_no_match(session):
    _seed(session)
    with pytest.raises(state.ResolveError) as e:
        state.resolve_name(session, "Mike Williams")
    assert len(e.value.candidates) == 2
    assert state.resolve_name(session, "Mike Williams", team="PIT").team == "PIT"
    with pytest.raises(state.ResolveError, match="no DB player"):
        state.resolve_name(session, "Mike Willyums")


# --- roster edits ----------------------------------------------------------

def test_roster_add_with_drop_logs_one_user_row(tmp_path, session):
    _seed(session)
    _write(tmp_path)
    ch = state.roster_add(session, "emmett johnson", slot="bench (claimed, $3)",
                          drop="DJ Moore", roster_path=str(tmp_path / "roster.csv"),
                          decisions_path=str(tmp_path / "decisions.csv"))
    assert (tmp_path / "roster.csv").read_text() == (
        "player,pos,team,slot\nDrake Maye,QB,NE,QB\nLA,DST,LA,DST\n"
        'Emmett Johnson,RB,KC,"bench (claimed, $3)"\n')
    dec = (tmp_path / "decisions.csv").read_text()
    assert dec.startswith(DECISIONS)
    last = next(csv.reader(io.StringIO(dec.splitlines()[-1])))
    assert last[0] == dt.date.today().isoformat() and last[1] == "10"  # next unplayed week
    assert last[2] == "Roster: added Emmett Johnson (RB KC, bench (claimed, $3)) / dropped D.J. Moore"
    assert last[3] == "user" and last[8] == "neutral"
    assert ch.row == 3
    assert not state.check_decisions(str(tmp_path / "decisions.csv"))


def test_roster_add_rejects_unknown_ambiguous_and_duplicate(tmp_path, session):
    _seed(session)
    _write(tmp_path)
    before = ((tmp_path / "roster.csv").read_bytes(), (tmp_path / "decisions.csv").read_bytes())
    kw = dict(roster_path=str(tmp_path / "roster.csv"),
              decisions_path=str(tmp_path / "decisions.csv"))
    with pytest.raises(state.ResolveError):
        state.roster_add(session, "Mike Williams", **kw)
    with pytest.raises(state.ResolveError):
        state.roster_add(session, "Nobody", **kw)
    with pytest.raises(ValueError, match="already on the roster"):
        state.roster_add(session, "Drake Maye", **kw)
    after = ((tmp_path / "roster.csv").read_bytes(), (tmp_path / "decisions.csv").read_bytes())
    assert after == before


def test_roster_move_and_drop_via_cli(tmp_path, session, capsys, monkeypatch):
    _write(tmp_path)
    db = tmp_path / "t.db"
    engine = create_db_engine(str(db))
    init_db(engine)
    with get_sessionmaker(engine)() as s:
        _seed(s)
    monkeypatch.chdir(tmp_path)
    assert main(["--db", str(db), "roster", "move", "dj moore", "--slot", "WR2"]) == 0
    assert main(["--db", str(db), "roster", "drop", "Drake Maye", "--week", "9",
                 "--why", "benched for performance"]) == 0
    assert (tmp_path / "roster.csv").read_text() == (
        "player,pos,team,slot\nLA,DST,LA,DST\nD.J. Moore,WR,BUF,WR2\n")
    rows = list(csv.reader(open(tmp_path / "decisions.csv", newline="")))
    assert rows[-2][2] == "Roster: moved D.J. Moore to WR2 (was bench (ankle, expected to play))"
    assert rows[-1][1:4] == ["9", "Roster: dropped Drake Maye", "user"]
    assert rows[-1][6] == "benched for performance"
    # Ambiguity fails loudly with candidates and leaves the files alone.
    assert main(["--db", str(db), "roster", "add", "Mike Williams"]) == 1
    out = capsys.readouterr().out
    assert out.count("candidate:") == 2
    assert main(["roster", "show"]) == 0


# --- decision log ----------------------------------------------------------

def test_log_add_and_grade(tmp_path, capsys):
    _write(tmp_path)
    path = str(tmp_path / "decisions.csv")
    assert main(["log", "add", "--decisions", path, "--week", "10", "--who", "claude",
                 "--decision", "Start Rice, over Corum", "--board", "Proj 14.1 vs 9.0",
                 "--override", "no", "--rationale", "board order"]) == 0
    rows = list(csv.reader(open(path, newline="")))
    assert rows[-1][1:] == ["10", "Start Rice, over Corum", "claude", "Proj 14.1 vs 9.0",
                            "no", "board order", "", "pending"]
    assert open(path, newline="").read().startswith(DECISIONS)
    # Bad override text is refused before anything is written.
    assert main(["log", "add", "--decisions", path, "--week", "10", "--who", "user",
                 "--decision", "x", "--board", "b", "--override", "maybe",
                 "--rationale", "r"]) == 1
    assert len(list(csv.reader(open(path, newline="")))) == 4
    # Grade the new row; a graded row needs --force.
    assert main(["log", "grade", "--decisions", path, "--row", "3",
                 "--outcome", "Rice 18.2", "--verdict", "hit"]) == 0
    assert list(csv.reader(open(path, newline="")))[-1][7:] == ["Rice 18.2", "hit"]
    assert main(["log", "grade", "--decisions", path, "--row", "2",
                 "--outcome", "x", "--verdict", "miss"]) == 1
    assert "already graded" in capsys.readouterr().out
    assert main(["log", "grade", "--decisions", path, "--row", "2", "--force",
                 "--outcome", "x", "--verdict", "miss"]) == 0
    # Untouched rows stay byte-identical through both edits.
    text = open(path, newline="").read()
    assert text.splitlines()[1] == DECISIONS.splitlines()[1]


def test_log_reader_treats_yes_prefix_as_override(tmp_path):
    """STATE.md's rule is 'override starts with yes/no'; the Log page reader
    must count 'YES (structural ...)' as an override, not only a bare yes."""
    from fantasy_football.matchups import read_decisions

    _write(tmp_path)
    log = read_decisions(str(tmp_path / "decisions.csv"))
    assert {r["t"]: r["ovr"] for r in log} == {"Start Maye": False, "Sit Moore": True}
