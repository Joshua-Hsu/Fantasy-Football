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
import urllib.error
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


def parse_sections(text: str, a: str, b: str, names: dict[str, tuple[str, str]] | None = None,
                   *, cap: bool = True) -> dict[str, str]:
    """Split a two-team answer into ``{abbr: note}`` by team headings.

    Headings are matched on the team's nickname (``Broncos``) or full name;
    everything after the last heading belongs to that team. Trailing
    "Would you like…" offers and source lists are dropped.
    """
    names = names or {}
    nick = {code: (names.get(code, (code, code))[0].split(" ")[-1]) for code in (a, b)}
    cur, buckets = None, {a: [], b: []}
    for raw in str(text or "").splitlines():
        # Heading detection happens on the RAW line: a markdown heading of any
        # length ("### Miami Dolphins Defense: Schematic Breakdown & Fantasy
        # Outlook") or a short bold/plain title naming the team. Then the
        # line is cleaned like the body.
        is_md_head = bool(re.match(r"^\s{0,3}#{1,6}\s", raw)) or bool(re.match(r"^\s*\*\*[^*]{3,90}\*\*\s*:?\s*$", raw))
        s = clean_text(raw).strip()
        if not s:
            continue
        head = None
        if is_md_head or len(s) < 60:
            for code, nk in nick.items():
                low = s.lower()
                if nk.lower() in low and (is_md_head or "defense" in low or low.endswith(nk.lower())
                                          or low == names.get(code, (code,))[0].lower()):
                    head = code
        if head:
            cur = head
            continue
        if s.lower().startswith(("would you like", "sources:", "[1]")) or re.match(r"^\[\d+\]", s):
            cur = None
            continue
        if cur:
            buckets[cur].append(s)
    joined = {code: re.sub(r"\s+", " ", " ".join(parts)).strip() for code, parts in buckets.items() if parts}
    return {code: (cap_note(v) if cap else v) for code, v in joined.items()}


_READ_RE = re.compile(r"Fantasy read:", re.I)
_NEXT_RE = re.compile(r"\b(?:Wk|Week)\s?\d+\b[^.]*", re.I)


def ensure_structure(checked: str, raw: str, cap: int = NOTE_CAP) -> str:
    """Guarantee the checked note keeps the 'Fantasy read:' sentence (and the
    next-opponent line when the raw section had one). The lite check pass
    sometimes rewrites them away; if so, take them from the raw section and
    append, trimming the body to fit the cap.
    """
    out = re.sub(r"\s+", " ", checked or "").strip()
    raw = re.sub(r"\s+", " ", raw or "")
    tail = ""
    if not _READ_RE.search(out):
        m = _READ_RE.search(raw)
        if m:
            tail = raw[m.start():].strip()
    if tail:
        tail = tail[:320].rsplit(".", 1)[0] + "." if len(tail) > 320 else tail
        out = cap_note(out, max(cap - len(tail) - 1, 200)) + " " + tail
    return cap_note(out, cap)


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

# The check a pasted note gets by hand (see the defense-notes skill): does the
# story contradict the box score? On the API path a second call does it.
_CHECK_PROMPT = (
    "Below is a fantasy scouting note about the {team} defense from NFL week {week} "
    "({year}), followed by the verified box-score lines of the players that defense "
    "faced in that game. Check every claim in the note against the lines. If the "
    "note says a defense shut something down that the numbers contradict (for "
    "example 'erased the run' when the back scored twice, or 'locked down the "
    "boundary' when the outside receiver went 5-101), rewrite that sentence to "
    "state the number and the correction. If a named stat line is wrong, fix it. "
    "Then rewrite the note to UNDER 850 characters total, in this order: scheme and "
    "personnel (package rates, blitz/pressure, who covered whom), what changed and "
    "why (injuries, fill-ins), what the offense did against them with the verified "
    "numbers, then one sentence starting exactly 'Fantasy read:' naming the positions "
    "this defense funnels production to and takes away, then one sentence starting "
    "'Wk{next_week} {next_opp}:' applying it to that opponent. Drop headings, "
    "citations and filler. Return ONLY the note text with no preamble."
    "\n\nNOTE:\n{note}\n\nVERIFIED LINES:\n{lines}"
)


def check_prompt(team: str, week: int, year: int, note: str, lines: list[str],
                 next_opp: str = "") -> str:
    return _CHECK_PROMPT.format(team=team, week=week, year=year, note=note,
                                next_week=week + 1, next_opp=next_opp or "(bye)",
                                lines="\n".join("- " + x for x in lines) or "(none)")


GEMINI_LIST_URL = "https://generativelanguage.googleapis.com/v1beta/models?key={key}&pageSize=200"


def pick_model(models: list[dict], prefer: str = "flash") -> str | None:
    """Choose the newest generally-available model whose name contains ``prefer``
    and that supports generateContent. Model names rotate ("gemini-2.5-flash"
    404s once retired), so the run asks the API instead of hard-coding one.
    Skips preview / experimental / lite / image / tts / embedding variants.
    """
    def ok(m):
        name = m.get("name", "").split("/")[-1]
        methods = m.get("supportedGenerationMethods") or []
        bad = ("preview", "exp", "lite", "image", "tts", "embed", "audio", "live", "thinking")
        return (prefer in name and "generateContent" in methods
                and not any(b in name for b in bad))

    def version(name: str) -> tuple:
        m = re.search(r"(\d+)\.(\d+)", name)
        return (int(m.group(1)), int(m.group(2))) if m else (0, 0)

    names = pick_models(models, prefer)
    return names[0] if names else None


def pick_models(models: list[dict], prefer: str = "flash") -> list[str]:
    """All acceptable models, newest first (``pick_model`` takes the head).

    The runner walks this list: the newest model may have no free-tier
    quota (a 429 that never clears), in which case the next one is tried.
    """
    def ok(m):
        name = m.get("name", "").split("/")[-1]
        methods = m.get("supportedGenerationMethods") or []
        bad = ("preview", "exp", "lite", "image", "tts", "embed", "audio", "live", "thinking")
        return (prefer in name and "generateContent" in methods
                and not any(b in name for b in bad))

    def version(name: str) -> tuple:
        m = re.search(r"(\d+)\.(\d+)", name)
        return (int(m.group(1)), int(m.group(2))) if m else (0, 0)

    return sorted((m.get("name", "").split("/")[-1] for m in models if ok(m)),
                  key=lambda n: (version(n), len(n)), reverse=True)


def gemini_models(api_key: str, timeout: int = 30) -> list[dict]:
    with urllib.request.urlopen(GEMINI_LIST_URL.format(key=api_key), timeout=timeout) as resp:
        return json.loads(resp.read().decode()).get("models", [])


def resolve_model(model: str, api_key: str) -> str:
    """``auto`` -> newest flash model the key can see; else the name as given."""
    if model != "auto":
        return model
    chosen = pick_model(gemini_models(api_key))
    if not chosen:
        raise RuntimeError("no Gemini flash model with generateContent is visible to this key")
    return chosen


class GeminiQuota(RuntimeError):
    """HTTP 429 that did not clear after the retries: try another model."""


def gemini_probe(api_key: str, model: str) -> dict:
    """One tiny PLAIN call to confirm the key and model work at all.

    Deliberately ungrounded: a grounded probe costs a billable grounded
    prompt every run. Grounded quota is diagnosed lazily instead - the first
    real scouting call that stays 429 after its retries prints the billing
    diagnosis (see ``scout-run``).
    """
    try:
        gemini_scout("Reply with the single word OK.", api_key, model=model,
                     grounded=False, retries=0, timeout=60, thinking_budget=0, max_output=8)
        return {"plain": "ok"}
    except GeminiQuota:
        return {"plain": "quota"}
    except Exception as exc:  # noqa: BLE001
        return {"plain": str(exc)[:160]}


# Published list prices (USD per 1M tokens) used only for the per-run
# estimate printed by ``scout-run``. Update here when Google changes them.
PRICES = {
    "flash": {"in": 0.75, "out": 3.75},        # 3.x Flash; thinking tokens bill as output
    "lite": {"in": 0.10, "out": 0.40},         # 3.x Flash-Lite
    "grounding_per_call": 0.014,               # $14 / 1k grounded prompts beyond the free 5k/month
}


def pick_lite_model(models: list[dict]) -> str | None:
    """Newest generally-available flash-lite model, for the cheap check pass."""
    def ok(m):
        name = m.get("name", "").split("/")[-1]
        methods = m.get("supportedGenerationMethods") or []
        bad = ("preview", "exp", "image", "tts", "embed", "audio", "live", "thinking")
        return ("flash" in name and "lite" in name and "generateContent" in methods
                and not any(b in name for b in bad))

    def version(name: str) -> tuple:
        m = re.search(r"(\d+)\.(\d+)", name)
        return (int(m.group(1)), int(m.group(2))) if m else (0, 0)

    names = sorted((m.get("name", "").split("/")[-1] for m in models if ok(m)),
                   key=lambda n: (version(n), len(n)), reverse=True)
    return names[0] if names else None


def estimate_cost(usage: dict) -> float:
    """Dollar estimate from an accumulated usage dict (see ``gemini_call``).

    Keys: ``flash_in``, ``flash_out`` (output + thinking), ``lite_in``,
    ``lite_out`` (token counts) and ``grounded`` (grounded call count).
    Grounding is priced as if metered - the free monthly allowance, when it
    applies, makes the real bill lower.
    """
    f, l = PRICES["flash"], PRICES["lite"]
    return (usage.get("flash_in", 0) * f["in"] + usage.get("flash_out", 0) * f["out"]
            + usage.get("lite_in", 0) * l["in"] + usage.get("lite_out", 0) * l["out"]) / 1e6 \
        + usage.get("grounded", 0) * PRICES["grounding_per_call"]


def gemini_call(prompt: str, api_key: str, *, model: str = "auto",
                timeout: int = 120, grounded: bool = True,
                retries: int = 3, backoff: float = 20.0,
                thinking_budget: int | None = None,
                max_output: int | None = None) -> tuple[str, dict]:
    """One Gemini call. Returns ``(text, usage)``.

    ``usage`` is ``{"in", "out", "thinking", "grounded"}`` from the response's
    usageMetadata (grounded = 1 when grounding metadata came back, i.e. the
    call is billable as a grounded prompt). ``thinking_budget`` caps the
    model's hidden reasoning tokens (0 = off) - they bill at the output rate
    and the check pass needs none. A 429 is retried with a growing pause; if
    it never clears ``GeminiQuota`` is raised so the caller can fall back to
    the next model. Other errors carry the API's message body.
    """
    import time

    model = resolve_model(model, api_key)
    gen: dict = {"temperature": 0.3}
    if thinking_budget is not None:
        gen["thinkingConfig"] = {"thinkingBudget": int(thinking_budget)}
    if max_output:
        gen["maxOutputTokens"] = int(max_output)
    body: dict = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": gen,
    }
    if grounded:
        body["tools"] = [{"google_search": {}}]
    payload = json.dumps(body).encode()
    for attempt in range(retries + 1):
        req = urllib.request.Request(
            GEMINI_URL.format(model=model) + f"?key={api_key}",
            data=payload, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode())
            break
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode()[:300]
            except Exception:  # noqa: BLE001
                pass
            if exc.code == 429 and attempt < retries:
                time.sleep(backoff * (attempt + 1))
                continue
            if exc.code in (500, 502, 503, 504) and attempt < retries:
                time.sleep(5 * (attempt + 1))  # transient - short pause, then again
                continue
            if exc.code == 429:
                raise GeminiQuota(f"{model}: HTTP 429 {detail}") from exc
            # 400 INVALID_ARGUMENT: some models reject a thinking budget (lite
            # models, or budget 0) or an output cap. Drop them and try again -
            # the call is still cheap, and a filed note beats a failed one.
            if exc.code == 400 and attempt < retries and (
                    "thinkingConfig" in gen or "maxOutputTokens" in gen):
                if "thinkingConfig" in gen:
                    gen.pop("thinkingConfig")
                else:
                    gen.pop("maxOutputTokens")
                payload = json.dumps(body).encode()
                continue
            raise RuntimeError(f"{model}: HTTP {exc.code} {detail}") from exc
    cand = data.get("candidates", [{}])[0]
    parts = cand.get("content", {}).get("parts", [])
    um = data.get("usageMetadata", {}) or {}
    usage = {"in": int(um.get("promptTokenCount", 0) or 0),
             "out": int(um.get("candidatesTokenCount", 0) or 0),
             "thinking": int(um.get("thoughtsTokenCount", 0) or 0),
             "grounded": 1 if cand.get("groundingMetadata") else 0}
    return "\n".join(p.get("text", "") for p in parts if p.get("text")), usage


def gemini_scout(prompt: str, api_key: str, **kw) -> str:
    """Text-only wrapper around ``gemini_call`` (kept for callers and tests)."""
    return gemini_call(prompt, api_key, **kw)[0]
