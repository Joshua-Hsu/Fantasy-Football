"""Weekly snapshots of Yahoo's salary-cap (auction) draft values.

Yahoo's public draft-analysis page (``?type=salcap``) lists, per player, the
**projected auction value** and the **current average cost** across live Yahoo
drafts — exactly the market signal to sanity-check our own Rec$ against. The
``pull-yahoo`` CLI command fetches it and writes:

- ``yahoo/values.<date>.csv`` — parsed rows (name, team/pos, projected, avg)
- ``yahoo/raw/<date>/salcap-<offset>.html`` — the raw pages, always saved, so
  if Yahoo changes markup the history can be re-parsed later

One dated CSV per run accumulates a trend series (and can feed the app's
cards later; join on player name, as the depth overrides do — Yahoo doesn't
expose our gsis keys). Yahoo blocks the Claude web sandbox's egress, so this
runs from GitHub Actions (see ``.github/workflows/yahoo-values.yml``), whose
runners have ordinary internet access.

Parsing is deliberately regex-loose (no HTML dependency): any table row that
links to a Yahoo player page yields a name, an optional ``Team - Pos`` note,
and every ``$``/``%`` token in the row, in document order. Projected value and
average cost are the first two dollar tokens (whichever order Yahoo lists,
both are captured; the CSV keeps both columns plus the raw token list).
"""

from __future__ import annotations

import re

SALCAP_URL = "https://football.fantasysports.yahoo.com/f1/draftanalysis?type=salcap"

#: Yahoo Fantasy Sports API (OAuth): per-player draft analysis, including
#: average auction cost — the numbers the web page locks behind Fantasy Plus
#: are open to any authenticated Yahoo account here.
TOKEN_URL = "https://api.login.yahoo.com/oauth2/get_token"
API_PLAYERS_URL = ("https://fantasysports.yahooapis.com/fantasy/v2/game/nfl/"
                   "players;start={start};count=25;sort=OR/draft_analysis"
                   "?format=json")

_ROW_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S | re.I)
_NAME_RE = re.compile(r"<a[^>]*(?:/nfl/players/|/nfl/teams/)[^>]*>([^<]{2,60})</a>", re.I)
_TEAMPOS_RE = re.compile(r"\b([A-Z][A-Za-z.]{1,4}\s*-\s*(?:QB|RB|WR|TE|K|DEF|DST))\b", re.I)
_DOLLAR_RE = re.compile(r"\$(\d+(?:\.\d+)?)")
_PCT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%")
_TAG_RE = re.compile(r"<[^>]+>")


def parse_salcap_html(html: str) -> list[dict]:
    """Extract salary-cap rows: name, team/pos, dollar and percent tokens."""
    out = []
    for row in _ROW_RE.findall(html or ""):
        name = _NAME_RE.search(row)
        if not name:
            continue
        dollars = [float(v) for v in _DOLLAR_RE.findall(row)]
        if not dollars:
            continue
        teampos = _TEAMPOS_RE.search(_TAG_RE.sub(" ", row))
        pcts = [float(v) for v in _PCT_RE.findall(row)]
        out.append({
            "name": name.group(1).strip(),
            "team_pos": teampos.group(1).strip() if teampos else "",
            "proj_value": dollars[0],
            "avg_cost": dollars[1] if len(dollars) > 1 else "",
            "all_dollars": "|".join(f"{v:g}" for v in dollars),
            "pct_drafted": pcts[0] if pcts else "",
        })
    return out


def _api_access_token(client_id: str, client_secret: str, refresh_token: str,
                      timeout: int = 30) -> str:
    """Trade the long-lived refresh token for a short-lived access token."""
    import json
    from urllib.parse import urlencode
    from urllib.request import Request, urlopen

    data = urlencode({
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "redirect_uri": "https://localhost",
        "grant_type": "refresh_token",
    }).encode()
    req = Request(TOKEN_URL, data=data,
                  headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())["access_token"]


def fetch_salcap_api(client_id: str, client_secret: str, refresh_token: str,
                     *, players: int = 350, timeout: int = 30) -> list[tuple[int, str]]:
    """Fetch draft-analysis JSON pages (25 players each) from the Fantasy API.

    Returns ``[(start_offset, json_text), ...]``; pages that error are skipped
    so a partial pull still lands (and the raw JSON is archived either way).
    """
    from urllib.request import Request, urlopen

    token = _api_access_token(client_id, client_secret, refresh_token,
                              timeout=timeout)
    from urllib.error import HTTPError

    pages = []
    for start in range(0, players, 25):
        req = Request(API_PLAYERS_URL.format(start=start),
                      headers={"Authorization": f"Bearer {token}"})
        try:
            with urlopen(req, timeout=timeout) as resp:
                pages.append((start, resp.read().decode("utf-8", "replace")))
        except HTTPError as exc:
            # Yahoo puts the useful part (e.g. scope problems) in the body.
            detail = exc.read().decode("utf-8", "replace")[:300]
            print(f"warning: api page start={start} failed: {exc} :: {detail}")
            if exc.code == 401 and start == 0:
                print("hint: a 401 with a valid token usually means the token "
                      "has no Fantasy Sports scope - re-authorize with "
                      "&scope=fspt-r and refresh YAHOO_REFRESH_TOKEN.")
                break  # every page will fail the same way
        except Exception as exc:  # noqa: BLE001 - keep pulling the other pages
            print(f"warning: api page start={start} failed: {exc}")
    return pages


def _flatten(node, out: dict) -> None:
    """Collapse Yahoo's deeply nested lists-of-single-key-dicts into one map."""
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(value, (dict, list)):
                _flatten(value, out)
            else:
                out.setdefault(key, value)
    elif isinstance(node, list):
        for item in node:
            _flatten(item, out)


def parse_players_api_json(text: str) -> list[dict]:
    """Extract salcap rows from a Fantasy API players;draft_analysis page.

    Tolerant of Yahoo's odd JSON shape (numbered dicts, single-key dict
    lists): each player subtree is flattened, then the fields we chart are
    picked out. ``proj_value`` is the preseason average auction cost (Yahoo's
    projection signal), ``avg_cost`` the live average across drafts.
    """
    import json

    try:
        payload = json.loads(text)
    except ValueError:
        return []

    players: list[dict] = []

    def walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "players" and isinstance(value, dict):
                    for idx, entry in value.items():
                        if not idx.isdigit() or not isinstance(entry, dict):
                            continue
                        flat: dict = {}
                        _flatten(entry.get("player", entry), flat)
                        if flat.get("full"):
                            players.append(flat)
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(payload)

    def num(value):
        try:
            return float(value)
        except (TypeError, ValueError):
            return ""

    out = []
    for flat in players:
        team = str(flat.get("editorial_team_abbr", "")).upper()
        pos = flat.get("display_position", "")
        proj = num(flat.get("preseason_average_cost"))
        avg = num(flat.get("average_cost"))
        pct = num(flat.get("percent_drafted"))
        out.append({
            "name": flat["full"],
            "team_pos": f"{team} - {pos}".strip(" -"),
            "proj_value": "" if proj in ("", -1.0) else proj,
            "avg_cost": "" if avg in ("", -1.0) else avg,
            "all_dollars": "|".join(f"{v:g}" for v in (proj, avg)
                                    if isinstance(v, float) and v >= 0),
            "pct_drafted": "" if pct == "" else (round(pct * 100, 1) if pct <= 1 else pct),
        })
    return out


def fetch_salcap_pages_browser(*, url: str = SALCAP_URL,
                               offsets=(0, 50, 100, 150, 200, 250),
                               timeout: int = 45) -> list[tuple[int, str]]:
    """Fetch the salcap pages with a rendered (headless Chromium) browser.

    Yahoo ships the page as a JS shell — the value table only exists after
    scripts run — so the static fetch sees no rows. This renders each page,
    waits for a player link to appear, and returns the RENDERED html, which
    the same ``parse_salcap_html`` handles. Needs the ``playwright`` package
    plus its Chromium (installed by the GitHub Action); a page that never
    shows a player link is still returned, so the raw snapshot captures
    whatever Yahoo served (login wall, block page) for diagnosis.
    """
    import os

    from playwright.sync_api import sync_playwright

    pages = []
    with sync_playwright() as pw:
        exe = os.environ.get("YAHOO_PW_CHROMIUM")  # override for odd sandboxes
        browser = pw.chromium.launch(executable_path=exe or None)
        page = browser.new_page()
        for offset in offsets:
            target = f"{url}&count={offset}" if offset else url
            try:
                page.goto(target, wait_until="domcontentloaded",
                          timeout=timeout * 1000)
                try:
                    page.wait_for_selector("a[href*='/nfl/players/']",
                                           timeout=timeout * 1000)
                except Exception:  # noqa: BLE001 - snapshot whatever rendered
                    print(f"warning: no player rows rendered at offset={offset}")
                pages.append((offset, page.content()))
            except Exception as exc:  # noqa: BLE001 - keep pulling other pages
                print(f"warning: salcap page offset={offset} failed: {exc}")
        browser.close()
    return pages


def fetch_salcap_pages(*, url: str = SALCAP_URL, offsets=(0, 50, 100, 150, 200, 250),
                       timeout: int = 30) -> list[tuple[int, str]]:
    """Fetch the salcap page at several row offsets (Yahoo paginates by count=).

    Returns ``[(offset, html), ...]`` for every page that answered 200. A page
    that errors is skipped, not fatal — whatever came back still gets saved.
    """
    from urllib.request import Request, urlopen

    pages = []
    for offset in offsets:
        target = f"{url}&count={offset}" if offset else url
        req = Request(target, headers={
            "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/126.0.0.0 Safari/537.36"),
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-US,en;q=0.9",
        })
        try:
            with urlopen(req, timeout=timeout) as resp:
                pages.append((offset, resp.read().decode("utf-8", "replace")))
        except Exception as exc:  # noqa: BLE001 - keep pulling the other pages
            print(f"warning: salcap page offset={offset} failed: {exc}")
    return pages
