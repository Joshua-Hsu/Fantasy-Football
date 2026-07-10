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
