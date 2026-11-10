"""The repo's hand-kept state files: schema, validation and safe edits.

``roster.csv``, ``decisions.csv``, ``injuries.csv`` and
``defense_profiles.csv`` are the memory a fresh session relies on (schemas
in ``STATE.md``). They are appended by hand and by Routines, and have
drifted before - a 10-column row silently dropped out of the log, and the
roster was wrong for ten days. This module gives them:

* :func:`check_state` - a validator (``cli check-state``) that returns a
  per-row problem list; errors fail the command (and the dvp Action),
  warnings print but pass.
* :class:`CsvDoc` - a byte-faithful CSV editor. Every record keeps the
  exact text it was read from; only rows that are changed or appended are
  re-serialised (``csv.writer``, ``QUOTE_MINIMAL``, the file's own line
  terminator - ``\\n`` for every file written here). An untouched file
  writes back with zero diff whatever quoting style its rows were typed in.
* Roster edits (``cli roster add|drop|move``) that resolve names against
  the DB and log each change as a ``who=user`` row in ``decisions.csv`` -
  no other code path writes ``roster.csv``.
* ``cli log add|grade`` so sessions stop hand-writing decision rows.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import os
import re
from dataclasses import dataclass, field

ROSTER_PATH = "roster.csv"
DECISIONS_PATH = "decisions.csv"
INJURIES_PATH = "injuries.csv"
PROFILES_PATH = "defense_profiles.csv"

ROSTER_HEADER = ["player", "pos", "team", "slot"]
DECISIONS_HEADER = ["date", "week", "decision", "who", "board", "override",
                    "rationale", "outcome", "verdict"]
INJURIES_HEADER = ["date", "player", "team", "pos", "status", "injury",
                   "timeline", "replacement", "note"]
PROFILES_HEADER = ["team", "date", "profile"]

ROSTER_POSITIONS = {"QB", "RB", "WR", "TE", "K", "DST"}
WHO = {"claude", "user"}
VERDICTS = {"pending", "hit", "miss", "neutral"}

# nflverse team codes for the current 32 franchises (the codes the schedule,
# stats and roster files use; LA = Rams, LV = Raiders, WAS = Commanders).
TEAM_CODES = frozenset(
    "ARI ATL BAL BUF CAR CHI CIN CLE DAL DEN DET GB HOU IND JAX KC "
    "LA LAC LV MIA MIN NE NO NYG NYJ PHI PIT SEA SF TB TEN WAS".split())

# Starting lineup: 1 QB / 2 RB / 2 WR / 1 TE / 1 FLEX (RB/WR/TE) / 1 K / 1 DEF.
STARTER_NEEDS = {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "K": 1, "DST": 1}
FLEX_POSITIONS = ("RB", "WR", "TE")
MAX_ACTIVE_PLAYERS = 15  # 9 starters + 6 bench; IR slots are extra

# Display order for ``roster show`` (by the slot's leading token).
SLOT_ORDER = ["QB", "RB", "WR", "TE", "FLEX", "K", "DST", "BENCH", "IR"]


# --------------------------------------------------------------------------
# Byte-faithful CSV document
# --------------------------------------------------------------------------

def _serialise(fields: list[str], eol: str) -> str:
    buf = io.StringIO()
    csv.writer(buf, lineterminator=eol, quoting=csv.QUOTE_MINIMAL).writerow(fields)
    return buf.getvalue()


@dataclass
class Record:
    """One CSV record: parsed fields + the exact text it came from."""

    fields: list[str]
    raw: str | None  # None once edited -> re-serialised on save
    line: int  # 1-based physical line where the record starts


@dataclass
class CsvDoc:
    """A CSV file held so that saving it changes only the edited rows.

    Read with ``newline=""`` so embedded newlines and CRLF survive; records
    keep their source text and are re-emitted verbatim unless edited.
    """

    path: str
    records: list[Record] = field(default_factory=list)
    eol: str = "\n"
    trailing_newline: bool = True

    @classmethod
    def load(cls, path: str) -> "CsvDoc":
        with open(path, newline="", encoding="utf-8") as fh:
            text = fh.read()
        return cls.parse(text, path)

    @classmethod
    def parse(cls, text: str, path: str = "") -> "CsvDoc":
        doc = cls(path=path)
        lines = re.findall(r"[^\n]*\n|[^\n]+$", text)
        if lines and lines[0].endswith("\r\n"):
            doc.eol = "\r\n"
        doc.trailing_newline = (not text) or text.endswith("\n")
        consumed = 0

        def feed():
            nonlocal consumed
            for ln in lines:
                consumed += 1
                yield ln

        reader = csv.reader(feed())
        start = 0
        for fields in reader:
            raw = "".join(lines[start:consumed])
            doc.records.append(Record(fields=fields, raw=raw, line=start + 1))
            start = consumed
        return doc

    # header / rows -------------------------------------------------------
    @property
    def header(self) -> list[str]:
        return self.records[0].fields if self.records else []

    def rows(self) -> list[Record]:
        """Data records (header excluded, blank lines included)."""
        return self.records[1:]

    def dicts(self) -> list[dict]:
        h = self.header
        return [dict(zip(h, r.fields)) for r in self.rows() if r.fields]

    # edits ---------------------------------------------------------------
    def append(self, fields: list[str]) -> Record:
        last_line = self.records[-1].line if self.records else 0
        rec = Record(fields=[str(f) for f in fields], raw=None, line=last_line + 1)
        self.records.append(rec)
        return rec

    def set_fields(self, rec: Record, fields: list[str]) -> None:
        rec.fields = [str(f) for f in fields]
        rec.raw = None

    def remove(self, rec: Record) -> None:
        self.records.remove(rec)

    def text(self) -> str:
        out: list[str] = []
        for i, rec in enumerate(self.records):
            if rec.raw is not None:
                piece = rec.raw
                # A last record that had no newline needs one before more rows.
                if i < len(self.records) - 1 and not piece.endswith("\n"):
                    piece += self.eol
            else:
                piece = _serialise(rec.fields, self.eol)
            out.append(piece)
        return "".join(out)

    def save(self, path: str | None = None) -> None:
        path = path or self.path
        tmp = f"{path}.tmp{os.getpid()}"
        with open(tmp, "w", newline="", encoding="utf-8") as fh:
            fh.write(self.text())
        os.replace(tmp, path)


# --------------------------------------------------------------------------
# Names
# --------------------------------------------------------------------------

_SUFFIX = re.compile(r"\s+(jr\.?|sr\.?|ii|iii|iv|v)$", re.IGNORECASE)


def norm_name(name: str) -> str:
    """Case/punctuation/suffix-insensitive key (same idea as scout._norm_name):
    ``"D.J. Moore"`` == ``"DJ Moore"``, ``"Marvin Harrison Jr."`` ==
    ``"marvin harrison"``, ``"De'Von Achane"`` == ``"Devon Achane"``."""
    import unicodedata
    n = "".join(c for c in unicodedata.normalize("NFKD", name or "") if not unicodedata.combining(c))
    n = _SUFFIX.sub("", n.strip())
    n = re.sub(r"[^a-z ]", "", n.lower())
    return " ".join(n.split())


@dataclass
class Resolved:
    name: str  # canonical display name (Player.full_name or team code)
    pos: str  # roster position
    team: str
    player_id: int | None = None


class ResolveError(ValueError):
    def __init__(self, message: str, candidates: list[str] | None = None):
        super().__init__(message)
        self.candidates = candidates or []


_FANTASY_POS = {"QB", "RB", "WR", "TE", "K"}
_POS_MAP = {"FB": "RB", "PK": "K", "HB": "RB"}


def _roster_pos(pos: str | None) -> str:
    p = (pos or "").upper()
    return _POS_MAP.get(p, p)


def _describe(p) -> str:
    return (f"{p.full_name} ({_roster_pos(p.position) or '?'}, "
            f"{p.current_team or '?'}{'' if p.active else ', inactive'}, id {p.id})")


def resolve_name(session, name: str, *, team: str | None = None) -> Resolved:
    """Resolve a roster name to a DB player (or a DST team code).

    A bare team code (``LA``) resolves to that team's defense. Players match
    ``Player.full_name`` via :func:`norm_name`; when several share the key the
    candidates narrow by ``team`` (if given), then to fantasy positions, then
    to active players. Raises :class:`ResolveError` (with the candidate list)
    on no match or a remaining tie.
    """
    from .models import Player

    raw = (name or "").strip()
    code = raw.upper()
    if code in TEAM_CODES:
        # DST rows are team codes (the teams table need not be loaded).
        return Resolved(name=code, pos="DST", team=code)

    key = norm_name(raw)
    if not key:
        raise ResolveError(f"empty player name {name!r}")
    pool = session.query(Player).all()
    cands = [p for p in pool if norm_name(p.full_name) == key]
    if not cands:
        last = key.split()[-1]
        near = sorted(_describe(p) for p in pool
                      if norm_name(p.full_name).split()[-1:] == [last]
                      and _roster_pos(p.position) in _FANTASY_POS)[:8]
        raise ResolveError(f"no DB player matches {raw!r}", near)

    def narrow(items, pred):
        sub = [p for p in items if pred(p)]
        return sub or items

    if team:
        cands = narrow(cands, lambda p: (p.current_team or "").upper() == team.upper())
    cands = narrow(cands, lambda p: _roster_pos(p.position) in _FANTASY_POS)
    cands = narrow(cands, lambda p: bool(p.active))
    if len(cands) > 1:
        raise ResolveError(f"{raw!r} is ambiguous ({len(cands)} DB players)",
                           [_describe(p) for p in cands])
    p = cands[0]
    return Resolved(name=p.full_name, pos=_roster_pos(p.position),
                    team=(p.current_team or "").upper(), player_id=p.id)


def _db_has_players(session) -> bool:
    from .models import Player

    return session is not None and session.query(Player.id).first() is not None


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------

@dataclass
class Problem:
    file: str
    line: int  # physical line (1-based); 0 = whole file
    message: str
    level: str = "error"  # or "warn"

    def __str__(self) -> str:
        where = f"{self.file}:{self.line}" if self.line else self.file
        tag = "ERROR" if self.level == "error" else "warn "
        return f"{tag} {where}: {self.message}"


def _iso(s: str) -> bool:
    try:
        dt.date.fromisoformat(s)
    except (TypeError, ValueError):
        return False
    return len(s) == 10


def _check_shape(doc: CsvDoc, fname: str, header: list[str]) -> list[Problem]:
    probs: list[Problem] = []
    if doc.header != header:
        probs.append(Problem(fname, 1, f"header must be {','.join(header)}; got "
                             f"{','.join(doc.header)}"))
    for rec in doc.rows():
        if not rec.fields:
            probs.append(Problem(fname, rec.line, "blank line", "warn"))
        elif len(rec.fields) != len(header):
            probs.append(Problem(
                fname, rec.line,
                f"{len(rec.fields)} columns, expected {len(header)} (an unquoted comma? "
                f"row starts {','.join(rec.fields[:3])[:60]!r})"))
    return probs


def check_decisions_row(fields: list[str]) -> list[str]:
    """Problems with one decisions.csv data row (empty list = valid)."""
    if len(fields) != len(DECISIONS_HEADER):
        return [f"{len(fields)} columns, expected {len(DECISIONS_HEADER)}"]
    r = dict(zip(DECISIONS_HEADER, fields))
    out = []
    if not _iso(r["date"]):
        out.append(f"date must be ISO YYYY-MM-DD, got {r['date']!r}")
    if not re.fullmatch(r"\d{1,2}", r["week"].strip()):
        out.append(f"week must be an integer, got {r['week']!r}")
    if r["who"] not in WHO:
        out.append(f"who must be claude|user, got {r['who']!r}")
    if r["verdict"] not in VERDICTS:
        out.append(f"verdict must be one of {'|'.join(sorted(VERDICTS))}, got {r['verdict']!r}")
    if not re.match(r"(yes|no)\b", r["override"].strip(), re.IGNORECASE):
        out.append(f"override must start with yes/no, got {r['override'][:40]!r}")
    if not r["decision"].strip():
        out.append("decision is empty")
    return out


def check_decisions(path: str) -> list[Problem]:
    fname = os.path.basename(path)
    doc = CsvDoc.load(path)
    probs = _check_shape(doc, fname, DECISIONS_HEADER)
    for rec in doc.rows():
        if len(rec.fields) == len(DECISIONS_HEADER):
            probs += [Problem(fname, rec.line, m) for m in check_decisions_row(rec.fields)]
    return probs


def check_injuries(path: str) -> list[Problem]:
    fname = os.path.basename(path)
    doc = CsvDoc.load(path)
    probs = _check_shape(doc, fname, INJURIES_HEADER)
    for rec in doc.rows():
        if len(rec.fields) == len(INJURIES_HEADER):
            if not _iso(rec.fields[0]):
                probs.append(Problem(fname, rec.line,
                                     f"date must be ISO YYYY-MM-DD, got {rec.fields[0]!r}"))
            if not rec.fields[1].strip():
                probs.append(Problem(fname, rec.line, "player is empty"))
    return probs


def check_profiles(path: str) -> list[Problem]:
    fname = os.path.basename(path)
    doc = CsvDoc.load(path)
    probs = _check_shape(doc, fname, PROFILES_HEADER)
    for rec in doc.rows():
        if len(rec.fields) != len(PROFILES_HEADER):
            continue
        team, date, text = rec.fields
        if not team or len(team) > 3 or team not in TEAM_CODES:
            probs.append(Problem(fname, rec.line, f"team must be a <=3-char team code, got {team!r}"))
        if not _iso(date):
            probs.append(Problem(fname, rec.line, f"date must be ISO YYYY-MM-DD, got {date!r}"))
        if not text.strip():
            probs.append(Problem(fname, rec.line, "profile is empty"))
    return probs


def is_ir(slot: str) -> bool:
    return slot.strip().upper().startswith("IR")


def check_roster(path: str, session=None) -> list[Problem]:
    """Shape + position/team codes; every player resolves to a DB player when
    the DB has players (an empty DB skips resolution with a warning)."""
    fname = os.path.basename(path)
    doc = CsvDoc.load(path)
    probs = _check_shape(doc, fname, ROSTER_HEADER)
    have_db = _db_has_players(session)
    if not have_db:
        probs.append(Problem(fname, 0, "DB has no players - name resolution skipped "
                             "(load a season to check names)", "warn"))
    unmatched: list[str] = []
    seen: dict[str, int] = {}
    counts = {p: 0 for p in ROSTER_POSITIONS}
    active = 0
    for rec in doc.rows():
        if len(rec.fields) != len(ROSTER_HEADER):
            continue
        player, pos, team, slot = rec.fields
        if pos not in ROSTER_POSITIONS:
            probs.append(Problem(fname, rec.line, f"pos must be one of "
                                 f"{'|'.join(sorted(ROSTER_POSITIONS))}, got {pos!r}"))
        if team not in TEAM_CODES:
            probs.append(Problem(fname, rec.line, f"team {team!r} is not a known team code"))
        if not slot.strip():
            probs.append(Problem(fname, rec.line, "slot is empty"))
        k = norm_name(player)
        if k in seen:
            probs.append(Problem(fname, rec.line, f"{player!r} is already on line {seen[k]}"))
        seen[k] = rec.line
        if pos == "DST" and player.strip().upper() != team:
            probs.append(Problem(fname, rec.line,
                                 f"DST row's player must be the team code ({team}), got {player!r}"))
        if not is_ir(slot):
            active += 1
            if pos in counts:
                counts[pos] += 1
        if have_db:
            try:
                res = resolve_name(session, player, team=team)
            except ResolveError as e:
                unmatched.append(player)
                cands = f" - candidates: {'; '.join(e.candidates)}" if e.candidates else ""
                probs.append(Problem(fname, rec.line, f"{e}{cands}"))
                continue
            if pos != "DST" and res.pos and res.pos != pos:
                probs.append(Problem(fname, rec.line,
                                     f"{player}: DB position is {res.pos}, roster says {pos}", "warn"))
            if pos != "DST" and res.team and res.team != team:
                probs.append(Problem(fname, rec.line,
                                     f"{player}: DB team is {res.team}, roster says {team}", "warn"))
    if unmatched:
        probs.append(Problem(fname, 0, "unmatched names: " + ", ".join(unmatched)))
    if active > MAX_ACTIVE_PLAYERS:
        probs.append(Problem(fname, 0, f"{active} non-IR players (league max "
                             f"{MAX_ACTIVE_PLAYERS})", "warn"))
    short = [f"{p} {counts[p]}/{n}" for p, n in STARTER_NEEDS.items() if counts[p] < n]
    flex_have = sum(counts[p] for p in FLEX_POSITIONS)
    flex_need = sum(STARTER_NEEDS[p] for p in FLEX_POSITIONS) + 1
    if flex_have < flex_need:
        short.append(f"RB/WR/TE {flex_have}/{flex_need} (FLEX)")
    if short:
        probs.append(Problem(fname, 0, "cannot field 9 starters from non-IR players: "
                             + ", ".join(short), "warn"))
    return probs


def check_state(root: str = ".", session=None) -> list[Problem]:
    """Validate every state file under ``root``; missing files are errors."""
    probs: list[Problem] = []
    checks = [(DECISIONS_PATH, check_decisions), (INJURIES_PATH, check_injuries),
              (PROFILES_PATH, check_profiles)]
    for name, fn in checks:
        path = os.path.join(root, name)
        if not os.path.exists(path):
            probs.append(Problem(name, 0, "file is missing"))
            continue
        probs += fn(path)
    path = os.path.join(root, ROSTER_PATH)
    if os.path.exists(path):
        probs += check_roster(path, session)
    else:
        probs.append(Problem(ROSTER_PATH, 0, "file is missing"))
    return probs


# --------------------------------------------------------------------------
# Decision log edits
# --------------------------------------------------------------------------

def decision_row(*, week, decision: str, who: str, board: str, override: str,
                 rationale: str, outcome: str = "", verdict: str = "pending",
                 date: str | None = None) -> list[str]:
    fields = [date or dt.date.today().isoformat(), str(week), decision, who, board,
              override, rationale, outcome, verdict]
    fields = [" ".join(str(f).split()) for f in fields]  # one physical line per row
    probs = check_decisions_row(fields)
    if probs:
        raise ValueError("; ".join(probs))
    return fields


def append_decision(path: str, fields: list[str]) -> int:
    """Append a validated row; returns its data-row number (1 = first row)."""
    doc = CsvDoc.load(path)
    if doc.header != DECISIONS_HEADER:
        raise ValueError(f"{path}: unexpected header {doc.header}")
    probs = check_decisions_row(fields)
    if probs:
        raise ValueError("; ".join(probs))
    doc.append(fields)
    doc.save()
    return len(doc.rows())


def grade_decision(path: str, row: int, *, outcome: str, verdict: str,
                   force: bool = False) -> list[str]:
    """Fill ``outcome``/``verdict`` on data row ``row`` (1-based). Refuses a
    row that is already graded unless ``force``. Returns the new fields."""
    if verdict not in VERDICTS:
        raise ValueError(f"verdict must be one of {'|'.join(sorted(VERDICTS))}")
    doc = CsvDoc.load(path)
    rows = doc.rows()
    if not 1 <= row <= len(rows):
        raise ValueError(f"row {row} out of range (1..{len(rows)})")
    rec = rows[row - 1]
    if len(rec.fields) != len(DECISIONS_HEADER):
        raise ValueError(f"row {row} has {len(rec.fields)} columns; fix it first")
    current = rec.fields[8]
    if current != "pending" and not force:
        raise ValueError(f"row {row} is already graded ({current}); pass --force to regrade")
    new = list(rec.fields)
    new[7] = " ".join(outcome.split())
    new[8] = verdict
    probs = check_decisions_row(new)
    if probs:
        raise ValueError("; ".join(probs))
    doc.set_fields(rec, new)
    doc.save()
    return new


def current_week(session=None, decisions_path: str = DECISIONS_PATH) -> int | None:
    """The week a roster move belongs to: the next unplayed week in the DB's
    latest season, else the newest integer week in the decision log."""
    if session is not None:
        from sqlalchemy import func

        from .models import Game

        year = session.query(func.max(Game.season_year)).scalar()
        if year is not None:
            wk = (session.query(func.min(Game.week))
                  .filter(Game.season_year == year, Game.home_score.is_(None)).scalar())
            if wk is not None:
                return int(wk)
    if os.path.exists(decisions_path):
        for rec in reversed(CsvDoc.load(decisions_path).rows()):
            if len(rec.fields) > 1 and rec.fields[1].strip().isdigit():
                return int(rec.fields[1])
    return None


# --------------------------------------------------------------------------
# Roster edits
# --------------------------------------------------------------------------

def _find(doc: CsvDoc, name: str) -> Record | None:
    key = norm_name(name)
    for rec in doc.rows():
        if rec.fields and norm_name(rec.fields[0]) == key:
            return rec
    return None


def _slot_key(slot: str) -> tuple[int, str]:
    tok = re.match(r"[A-Za-z]+", slot.strip())
    head = tok.group(0).upper() if tok else ""
    for i, s in enumerate(SLOT_ORDER):
        if head == s:
            return i, slot
    return len(SLOT_ORDER), slot


def roster_groups(path: str = ROSTER_PATH) -> list[tuple[str, list[dict]]]:
    """Rows grouped by the slot's leading token (QB, RB, ..., BENCH, IR)."""
    doc = CsvDoc.load(path)
    groups: dict[str, list[dict]] = {}
    for r in doc.dicts():
        tok = re.match(r"[A-Za-z]+", r.get("slot", "").strip())
        head = tok.group(0).upper() if tok else "?"
        head = re.sub(r"\d+$", "", head) or head
        groups.setdefault(head, []).append(r)
    order = {s: i for i, s in enumerate(SLOT_ORDER)}
    return sorted(groups.items(), key=lambda kv: order.get(kv[0], len(order)))


@dataclass
class RosterChange:
    summary: str  # "Roster: added X / dropped Y"
    row: int  # decisions.csv data-row number of the log entry


def _roster_log_row(summary: str, week, rationale: str) -> list[str]:
    """The ``who=user`` decisions.csv row every roster change appends. It is
    a record of the move, not a call, so it is born graded ``neutral``."""
    return decision_row(week=week, decision=summary, who="user",
                        board="n/a (roster change)", override="no",
                        rationale=rationale,
                        outcome="Roster change record; the call behind it is logged separately",
                        verdict="neutral")


def roster_add(session, name: str, *, slot: str = "bench", drop: str | None = None,
               team: str | None = None, week=None, rationale: str = "",
               roster_path: str = ROSTER_PATH,
               decisions_path: str = DECISIONS_PATH) -> RosterChange:
    doc = CsvDoc.load(roster_path)
    if doc.header != ROSTER_HEADER:
        raise ValueError(f"{roster_path}: header must be {','.join(ROSTER_HEADER)}")
    res = resolve_name(session, name, team=team)
    if res.pos not in ROSTER_POSITIONS:
        raise ResolveError(f"{res.name} is a {res.pos or '?'} - not a rosterable position")
    if res.team not in TEAM_CODES:
        raise ResolveError(f"{res.name} has no current team in the DB ({res.team or 'none'})")
    if _find(doc, res.name) is not None:
        raise ValueError(f"{res.name} is already on the roster")
    dropped = None
    if drop:
        rec = _find(doc, drop)
        if rec is None:
            raise ValueError(f"{drop!r} is not on the roster")
        dropped = rec.fields[0]
        doc.remove(rec)
    doc.append([res.name, res.pos, res.team, slot.strip() or "bench"])
    week = week if week is not None else current_week(session, decisions_path)
    if week is None:
        raise ValueError("cannot infer the week; pass --week")
    summary = f"Roster: added {res.name} ({res.pos} {res.team}, {slot})"
    if dropped:
        summary += f" / dropped {dropped}"
    # Build (and validate) the log row before touching roster.csv.
    log = _roster_log_row(summary, week, rationale or "roster.csv edit via cli roster add")
    doc.save()
    return RosterChange(summary, append_decision(decisions_path, log))


def roster_drop(session, name: str, *, week=None, rationale: str = "",
                roster_path: str = ROSTER_PATH,
                decisions_path: str = DECISIONS_PATH) -> RosterChange:
    doc = CsvDoc.load(roster_path)
    res = resolve_name(session, name)
    rec = _find(doc, res.name) or _find(doc, name)
    if rec is None:
        raise ValueError(f"{res.name} is not on the roster")
    week = week if week is not None else current_week(session, decisions_path)
    if week is None:
        raise ValueError("cannot infer the week; pass --week")
    summary = f"Roster: dropped {rec.fields[0]}"
    log = _roster_log_row(summary, week, rationale or "roster.csv edit via cli roster drop")
    doc.remove(rec)
    doc.save()
    return RosterChange(summary, append_decision(decisions_path, log))


def roster_move(session, name: str, *, slot: str, week=None, rationale: str = "",
                roster_path: str = ROSTER_PATH,
                decisions_path: str = DECISIONS_PATH) -> RosterChange:
    if not slot.strip():
        raise ValueError("--slot is required")
    doc = CsvDoc.load(roster_path)
    res = resolve_name(session, name)
    rec = _find(doc, res.name) or _find(doc, name)
    if rec is None:
        raise ValueError(f"{res.name} is not on the roster")
    week = week if week is not None else current_week(session, decisions_path)
    if week is None:
        raise ValueError("cannot infer the week; pass --week")
    old = rec.fields[3] if len(rec.fields) > 3 else ""
    new = list(rec.fields)
    new[3] = slot.strip()
    summary = f"Roster: moved {rec.fields[0]} to {slot.strip()} (was {old})"
    log = _roster_log_row(summary, week, rationale or "roster.csv edit via cli roster move")
    doc.set_fields(rec, new)
    doc.save()
    return RosterChange(summary, append_decision(decisions_path, log))
