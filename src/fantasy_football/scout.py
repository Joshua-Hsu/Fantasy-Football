"""Weekly defensive scouting loop (``defense_notes.csv``).

The Matchups page carries one hand-written scouting note per defense per
week: scheme, personnel, what changed, and the fantasy translation. The
notes come from a grounded LLM (Gemini with web search worked best) asked
a natural fan question about BOTH defenses in one game, then checked
against the box score in our database before they are filed. This module
makes that loop mechanical:

- ``game_pairs``      the played games of a week with next week's opponents
- ``box_lines``       the verified skill-player lines a defense faced
- ``scout_prompt``    the fan-style question (with the box lines appended so
                      the model anchors on real numbers instead of inventing)
- ``parse_sections``  split a two-team answer into per-team notes, capped
- ``gemini_scout``    optional fully-automated path via the Gemini API with
                      Google Search grounding (needs GEMINI_API_KEY; runs in
                      Actions, not in the web sandbox)

Notes are appended, never edited: ``read_defense_notes`` shows the newest
per team, so a corrected note simply supersedes the old one.
"""

from __future__ import annotations

import csv
import datetime as dt
import json
import os
import re
import urllib.request

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Game, Player, PlayerGameStats, Team

NOTE_CAP = 900  # matches read_defense_notes

# The wording that got full two-team answers out of Gemini lives in
# prompts/defense_scout.txt (edit THAT, with a changelog in prompts/README.md).
# A terse "scout X for fantasy" prompt came back as one thin paragraph; this
# reads like a person asking and came back with scheme, personnel and
# numbers. The copy below is only the fallback if the file is missing.
PROMPT_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "prompts", "defense_scout.txt")

_PROMPT_FALLBACK = (
    "I'm in a fantasy football league and trying to get a real feel for the "
    "{a} and {b} defenses after their week {week} game this season ({year}). "
    "Not just the box score, more how each defense actually played. Were they "
    "mostly in nickel or base? Rushing four or blitzing? Who was on the outside "
    "receivers versus the slot versus the tight end, and how many guys were "
    "they keeping in the box against the run? Did either defense change "
    "anything from week {prev}, and was that a game plan thing or because of "
    "injuries? Who was hurt or back on defense and who filled in for them? And "
    "what was the other offense able to do against them, if anything?\n\n"
    "Then the part I care about most: which positions do these two defenses "
    "give up fantasy points to, and which do they shut down? {a_short} plays "
    "{a_next} next week and {b_short} plays {b_next}, so how does that apply? "
    "Snap shares, pressure rates, targets allowed, anything with numbers is "
    "great. A couple of solid paragraphs on each team is perfect."
)


def prompt_template(path: str | None = None) -> str:
    """The scouting question template: the prompts/ file, else the fallback."""
    path = path or PROMPT_FILE
    try:
        text = open(path, encoding="utf-8").read().strip()
        return text or _PROMPT_FALLBACK
    except OSError:
        return _PROMPT_FALLBACK


def _team_names(session: Session) -> dict[str, tuple[str, str]]:
    """``{abbr: (full name, short name)}`` e.g. ``("Denver Broncos", "Denver")``."""
    out = {}
    for t in session.execute(select(Team)).scalars():
        loc = (t.location or "").strip()
        # Some loads store the full name in ``location`` ("Atlanta Falcons");
        # don't double the nickname in that case.
        full = loc if loc.endswith(t.name) else (f"{loc} {t.name}".strip() if loc else t.name)
        short = full[: -len(t.name)].strip() if full.endswith(t.name) and full != t.name else t.name
        out[t.abbreviation] = (full, short)
    return out


def game_pairs(session: Session, year: int, week: int) -> list[dict]:
    """The week's played games, each with both teams' next-week opponents.

    ``[{"a": "DEN", "b": "JAX", "a_next": "vLA", "b_next": "vNE", ...}]``
    ordered as scheduled. Unplayed games (no score) are skipped; a team on
    bye next week gets ``"bye"``.
    """
    from .matchups import week_opponents

    abbr = {t.id: t.abbreviation for t in session.execute(select(Team)).scalars()}
    nxt = week_opponents(session, year, week + 1)
    games = session.execute(
        select(Game).where(Game.season_year == year, Game.week == week,
                           Game.season_type == "regular")
        .order_by(Game.game_date, Game.id)).scalars().all()
    out = []
    for g in games:
        if g.home_score is None and g.away_score is None:
            continue
        a, b = abbr[g.home_team_id], abbr[g.away_team_id]
        out.append({"a": a, "b": b, "a_next": nxt.get(a, "bye"), "b_next": nxt.get(b, "bye"),
                    "score": f"{b} {g.away_score} @ {a} {g.home_score}"})
    return out


def box_lines(session: Session, year: int, week: int, defense: str) -> list[str]:
    """The skill-player lines a defense faced in one week, from our box scores.

    These are the facts a scouting paste is checked against (and appended to
    the prompt so the model cannot invent them): every QB plus any RB/WR/TE
    with 3+ touches or targets, sorted by volume.
    """
    abbr = {t.id: t.abbreviation for t in session.execute(select(Team)).scalars()}
    dteam = next((tid for tid, ab in abbr.items() if ab == defense), None)
    if dteam is None:
        return []
    game = session.execute(
        select(Game).where(Game.season_year == year, Game.week == week,
                           (Game.home_team_id == dteam) | (Game.away_team_id == dteam))
    ).scalars().first()
    if game is None:
        return []
    opp = game.away_team_id if game.home_team_id == dteam else game.home_team_id
    rows = session.execute(
        select(PlayerGameStats, Player)
        .join(Player, Player.id == PlayerGameStats.player_id)
        .where(PlayerGameStats.game_id == game.id, PlayerGameStats.team_id == opp)
    ).all()
    lines = []
    for st, p in rows:
        pos = (st.position or p.position or "").upper()
        vol = st.targets + st.rush_attempts
        if pos == "QB" and st.pass_attempts:
            lines.append((99, f"{p.full_name} (QB) {st.pass_completions}/{st.pass_attempts} "
                              f"{st.pass_yards} yds {st.pass_touchdowns} TD"
                              + (f", {st.rush_attempts} car {st.rush_yards} yds "
                                 f"{st.rush_touchdowns} TD" if st.rush_attempts else "")))
        elif pos in ("RB", "WR", "TE") and vol >= 3:
            parts = []
            if st.rush_attempts:
                parts.append(f"{st.rush_attempts} car {st.rush_yards} yds {st.rush_touchdowns} TD")
            if st.targets:
                parts.append(f"{st.targets} tgt {st.receptions} rec {st.receiving_yards} yds "
                             f"{st.receiving_touchdowns} TD")
            lines.append((vol, f"{p.full_name} ({pos}) " + ", ".join(parts)))
    lines.sort(key=lambda x: -x[0])
    return [text for _, text in lines]


def scout_prompt(session: Session, year: int, week: int, a: str, b: str,
                 *, a_next: str, b_next: str, with_lines: bool = True) -> str:
    """The fan-style question for one game, both defenses."""
    names = _team_names(session)
    fa, sa = names.get(a, (a, a))
    fb, sb = names.get(b, (b, b))

    def opp_name(code: str) -> str:
        if code == "bye":
            return "nobody (bye week)"
        tm = code.lstrip("@v")
        return ("at the " if code.startswith("@") else "the ") + names.get(tm, (tm, tm))[0].split(" ")[-1]

    text = prompt_template().format(a=fa, b=fb, a_short=sa, b_short=sb, week=week, prev=max(week - 1, 1),
                          year=year, a_next=opp_name(a_next), b_next=opp_name(b_next))
    if with_lines:
        la, lb = box_lines(session, year, week, a), box_lines(session, year, week, b)
        if la or lb:
            text += ("\n\nFor reference, here are the actual box score lines from that game "
                     "so the numbers line up:\n")
            if la:
                text += f"\nAgainst the {fa} defense:\n" + "\n".join("- " + x for x in la) + "\n"
            if lb:
                text += f"\nAgainst the {fb} defense:\n" + "\n".join("- " + x for x in lb) + "\n"
    return text


_MD_LINK = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_CITE = re.compile(r"\s*\[\d+(?:,\s*\d+)*\]")


def clean_text(text: str) -> str:
    """Strip markdown links, bracket citations and heading markup."""
    text = _MD_LINK.sub(r"\1", text)
    text = _CITE.sub("", text)
    text = re.sub(r"\*\*|__|^#+\s*", "", text, flags=re.M)
    return re.sub(r"[ \t]+", " ", text).strip()


def cap_note(text: str, cap: int = NOTE_CAP) -> str:
    """Trim to the note cap at a sentence boundary where possible."""
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= cap:
        return text
    cut = text[:cap]
    for sep in (". ", "; ", ", "):
        i = cut.rfind(sep)
        if i > cap * 0.6:
            return cut[: i + 1].strip()
    return cut.rstrip() + "..."


def parse_sections(text: str, a: str, b: str, names: dict[str, tuple[str, str]] | None = None) -> dict[str, str]:
    """Split a two-team answer into ``{abbr: note}`` by team headings.

    Headings are matched on the team's nickname (``Broncos``) or full name;
    everything after the last heading belongs to that team. Trailing
    "Would you like…" offers and source lists are dropped.
    """
    names = names or {}
    nick = {code: (names.get(code, (code, code))[0].split(" ")[-1]) for code in (a, b)}
    lines = clean_text(text).splitlines()
    cur, buckets = None, {a: [], b: []}
    for ln in lines:
        s = ln.strip()
        if not s:
            continue
        head = None
        if len(s) < 60:
            for code, nk in nick.items():
                if nk.lower() in s.lower() and ("defense" in s.lower() or s.lower().endswith(nk.lower())
                                                or s.lower() == names.get(code, (code,))[0].lower()):
                    head = code
        if head:
            cur = head
            continue
        if s.lower().startswith(("would you like", "sources:", "[1]")) or re.match(r"^\[\d+\]", s):
            cur = None
            continue
        if cur:
            buckets[cur].append(s)
    return {code: cap_note(" ".join(parts)) for code, parts in buckets.items() if parts}


def append_notes(path: str, notes: dict[str, str], date: str | None = None) -> int:
    date = date or dt.date.today().isoformat()
    n = 0
    with open(path, "a", newline="") as f:
        w = csv.writer(f)
        for team, note in notes.items():
            if note:
                w.writerow([team, date, note])
                n += 1
    return n


def noted_teams(path: str, since: str) -> set[str]:
    """Teams that already have a note dated on/after ``since`` (YYYY-MM-DD)."""
    if not os.path.exists(path):
        return set()
    out = set()
    for r in csv.DictReader(open(path, newline="")):
        if (r.get("date") or "") >= since:
            out.add((r.get("team") or "").strip().upper())
    return out


GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


def gemini_scout(prompt: str, api_key: str, *, model: str = "gemini-2.5-flash",
                 timeout: int = 120) -> str:
    """One grounded Gemini call (Google Search tool on). Returns the text."""
    body = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "tools": [{"google_search": {}}],
        "generationConfig": {"temperature": 0.3},
    }
    req = urllib.request.Request(
        GEMINI_URL.format(model=model) + f"?key={api_key}",
        data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode())
    parts = data.get("candidates", [{}])[0].get("content", {}).get("parts", [])
    return "\n".join(p.get("text", "") for p in parts if p.get("text"))
