"""Draft-board export: a tiered cheat sheet as an Excel workbook.

One sheet per position plus an overall sheet. Within a position, players are
ordered by their user rating (the head-to-head result), grouped into tiers, with
both their rank-in-position and rank-in-tier shown alongside the auction value
and the three stat bases. Tiers reflect the user ratings, so the board sharpens
as you make picks.

openpyxl is an optional dependency (``pip install -e ".[export]"``).
"""

from __future__ import annotations

from typing import NamedTuple

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import UserRating
from .ratings import seed_ratings, user_rating_tiers
from .scoring import DEFAULT_RULES, ScoringRules
from .valuation import (
    ALL_POSITIONS,
    DEFAULT_LEAGUE,
    LeagueConfig,
    _latest_season,
    compute_values,
)

# A few soft fills cycled per tier so tier bands are easy to scan.
_TIER_FILLS = ["FFF6E5", "E8F0FE", "E9F7EF", "FCE8F3", "F3E8FD", "EAF2F8", "FFF0E6"]

_HEADERS = ["Tier", "PosRank", "TierRank", "Player", "Tm", "Ovr", "$", "UserRtg",
            "LastYr", "PPG", "3yrWtd"]


class BoardRow(NamedTuple):
    key: str
    tier: int
    pos_rank: int
    tier_rank: int
    name: str
    team: str
    overall_rank: int
    dollars: float
    user_rating: float
    total: float
    ppg: float
    w3yr: float
    position: str
    is_rookie: bool


def build_board(
    session: Session,
    *,
    year: int | None = None,
    config: LeagueConfig = DEFAULT_LEAGUE,
    rules: ScoringRules = DEFAULT_RULES,
    basis: str = "w3yr",
    manual_tiers: dict[str, int] | None = None,
    fixed_prices: dict[str, float] | None = None,
) -> dict[str, list[BoardRow]]:
    """Per-position rows grouped by tier, with within-tier and overall ranks.

    Tiers come from the head-to-head user ratings; any explicit ``manual_tiers``
    (hand-set or exported from the pick game) are shown verbatim, taking
    precedence. ``fixed_prices`` pins expected market prices for specific
    players; the field re-prices around them.
    """
    seed_ratings(session, year=year, config=config, rules=rules, basis=basis)
    tiers = user_rating_tiers(session)
    if manual_tiers:
        tiers = {**tiers, **manual_tiers}
    values = compute_values(
        session, year=year, config=config, rules=rules, basis=basis, manual_tiers=tiers,
        fixed_prices=fixed_prices,
    )
    ratings = {r.key: r.rating for r in session.scalars(select(UserRating))}

    board: dict[str, list[BoardRow]] = {}
    for pos in ALL_POSITIONS:
        rows = values.get(pos, [])
        if not rows:
            continue
        # Group by tier (contiguous), best rating first within each tier.
        ranked = sorted(rows, key=lambda r: (r.tier, -ratings.get(r.key, r.basis_value)))
        per_tier_seen: dict[int, int] = {}
        out: list[BoardRow] = []
        for i, r in enumerate(ranked, 1):
            per_tier_seen[r.tier] = per_tier_seen.get(r.tier, 0) + 1
            out.append(
                BoardRow(
                    key=r.key, tier=r.tier, pos_rank=i, tier_rank=per_tier_seen[r.tier],
                    name=r.name, team=r.team, overall_rank=r.overall_rank,
                    dollars=r.dollars,
                    user_rating=round(ratings.get(r.key, r.basis_value), 1),
                    total=r.total, ppg=r.ppg, w3yr=r.w3yr, position=pos,
                    is_rookie=r.is_rookie,
                )
            )
        board[pos] = out
    return board


#: Default pick-game depth per position — roughly the draftable universe for a
#: 12-team league plus a buffer. Trims the long tail of players unlikely to be
#: drafted so you never have to weigh them head-to-head.
DEFAULT_WEBAPP_DEPTH = {"QB": 24, "RB": 48, "WR": 54, "TE": 24, "K": 16, "DST": 32}

#: Positions limited to one per team (NFL starters) rather than a depth cap.
#: QB/K backups don't get drafted, so we keep only each team's starter. DST is
#: left uncapped (32) so every defense is ranked.
STARTER_POSITIONS = {"QB", "K"}


def build_webapp_data(
    session: Session,
    *,
    year: int | None = None,
    config: LeagueConfig = DEFAULT_LEAGUE,
    rules: ScoringRules = DEFAULT_RULES,
    basis: str = "w3yr",
    depth: int | dict[str, int] | None = None,
    rookie_max_round: int = 3,
    manual_tiers: dict[str, int] | None = None,
) -> dict[str, list[dict]]:
    """Per-position player data for the static pick game (browser app).

    Each entity carries the three comparison stats, a seed value (for the
    in-browser Elo), current team, and coaching. No DB needed at play time.

    ``depth`` caps how many *veterans* per position are included (top N by value),
    so the long tail of undraftable players is left out. Incoming rookies drafted
    in rounds 1..``rookie_max_round`` are added on top (seeded by draft capital so
    they interleave with veterans), since they have no stats to rank them.
    ``manual_tiers`` (hand-set tiers, by key) pins those players to their tier and
    seeds them by it so the game/ranking reflect the hard-set order.
    """
    from .models import Player, Team

    manual_tiers = manual_tiers or {}
    if isinstance(depth, int):
        caps = {pos: depth for pos in ALL_POSITIONS}
    else:
        caps = {**DEFAULT_WEBAPP_DEPTH, **(depth or {})}

    values = compute_values(
        session, year=year, config=config, rules=rules, basis=basis, manual_tiers=manual_tiers
    )
    coaching = {
        t.abbreviation: (t.head_coach or "", t.offensive_coordinator or "", t.play_caller or "")
        for t in session.scalars(select(Team))
    }
    draft = {
        f"p{pid}": (rnd, pick)
        for pid, rnd, pick in session.execute(
            select(Player.id, Player.draft_round, Player.draft_pick).where(Player.draft_round.isnot(None))
        )
    }
    # Last-year stat lines for the cards / CSV export.
    last_year = year or _latest_season(session)
    pstats = _player_last_year(session, last_year) if last_year else {}
    toff = _team_offense(session, last_year) if last_year else {}
    dstats = _dst_last_year(session, last_year) if last_year else {}

    # Where a rookie of each round slots into a position's value scale.
    round_slot = {1: 7, 2: 17, 3: 27}

    def seed_for(r, fallback):
        # Hand-set tiers pin the seed by tier (tier 1 highest), value as tiebreak.
        if r.key in manual_tiers:
            return (8 - manual_tiers[r.key]) * 100 + min(r.basis_value, 99) * 0.001
        return fallback

    def emit(r, seed):
        hc, oc, pc = coaching.get(r.team, ("", "", ""))
        rnd, pick = draft.get(r.key, (None, None))
        row = {
            "key": r.key, "name": r.name, "team": r.team, "pos": r.position,
            "total": r.total, "ppg": r.ppg, "w3yr": r.w3yr,
            "seed": round(seed, 1), "rookie": r.is_rookie, "hc": hc, "oc": oc, "pc": pc,
            "stat": _format_statline(r.key, r.position, pstats, dstats),
            "tmoff": _format_team_context(r.team, r.position, toff),
        }
        # Manual tiers only seed the starting `seed` (above) — the app's Elo
        # refines from there, so we deliberately don't lock a tier here.
        if r.is_rookie and rnd:
            row["draft"] = f"R{rnd} P{pick}"
        return row

    def rookie_seed(r, vet_seeds):
        rnd, pick = draft.get(r.key, (None, None))
        idx = round_slot.get(rnd, 30)
        base = vet_seeds[min(idx, len(vet_seeds) - 1)] if vet_seeds else 0.0
        return base - (pick or 0) * 0.001  # earlier picks rank a touch higher

    out: dict[str, list[dict]] = {}
    for pos in ALL_POSITIONS:
        rows = values.get(pos, [])  # sorted by value desc
        vets = [r for r in rows if not r.is_rookie]
        vet_seeds = [r.basis_value for r in vets]
        rookies = [
            r for r in rows
            if r.is_rookie and (draft.get(r.key, (99,))[0] or 99) <= rookie_max_round
        ]

        if pos in STARTER_POSITIONS:
            # One per team (the projected starter): best seed per current team.
            best: dict[str, tuple] = {}
            for r in vets + rookies:
                base = r.basis_value if not r.is_rookie else rookie_seed(r, vet_seeds)
                seed = seed_for(r, base)
                team = r.team or r.key
                if team not in best or seed > best[team][1]:
                    best[team] = (r, seed)
            chosen = list(best.values())
        else:
            chosen = [(r, seed_for(r, r.basis_value)) for r in vets[: caps.get(pos)]]
            chosen += [(r, seed_for(r, rookie_seed(r, vet_seeds))) for r in rookies]

        chosen.sort(key=lambda x: x[1], reverse=True)
        out[pos] = [emit(r, s) for r, s in chosen]
    return out


def write_webapp_data(
    session: Session,
    path: str,
    *,
    year: int | None = None,
    config: LeagueConfig = DEFAULT_LEAGUE,
    rules: ScoringRules = DEFAULT_RULES,
    basis: str = "w3yr",
    depth: int | dict[str, int] | None = None,
    manual_tiers: dict[str, int] | None = None,
) -> str:
    """Write the pick-game data as ``docs/data.js`` (``window.FF_DATA = {...}``)."""
    import json

    data = build_webapp_data(
        session, year=year, config=config, rules=rules, basis=basis, depth=depth,
        manual_tiers=manual_tiers,
    )
    payload = {"basis": basis, "positions": data}
    with open(path, "w") as fh:
        fh.write("// Generated by `fantasy_football build-webapp` - do not edit by hand.\n")
        fh.write("window.FF_DATA = ")
        json.dump(payload, fh, ensure_ascii=False)
        fh.write(";\n")
    return path


# Last-year stat columns shown inline per position: (header, PlayerGameStats attr).
_SKILL_STATS = [
    ("Car", "rush_attempts"), ("RuYds", "rush_yards"), ("RuTD", "rush_touchdowns"),
    ("Tgt", "targets"), ("Rec", "receptions"), ("ReYds", "receiving_yards"),
    ("ReTD", "receiving_touchdowns"),
]
STAT_COLS: dict[str, list[tuple[str, str]]] = {
    "QB": [("PaAtt", "pass_attempts"), ("PaYds", "pass_yards"), ("PaTD", "pass_touchdowns"),
           ("INT", "interceptions_thrown"), ("RuAtt", "rush_attempts"), ("RuYds", "rush_yards")],
    "RB": _SKILL_STATS, "WR": _SKILL_STATS, "TE": _SKILL_STATS,
    "K": [("FGM", "field_goals_made"), ("FGA", "field_goals_attempted"), ("XPM", "extra_points_made")],
    "DST": [("PA/g", "PA"), ("Sack", "Sack"), ("INT", "INT"), ("DefTD", "TD")],
}
#: Positions that show their team's offensive context inline.
TEAM_OFFENSE_POSITIONS = {"QB", "RB", "WR", "TE"}
_OFFENSE_METRICS = [("TmYds", "total_yards"), ("TmPlays", "plays"), ("TmRush", "rush"), ("TmPass", "pass")]


def _player_last_year(session: Session, year: int) -> dict[str, dict[str, int]]:
    """{p<id>: {stat: 2025 regular-season total}} for all the stats we display."""
    from .models import Game, Player, PlayerGameStats

    cols = sorted({attr for cols in STAT_COLS.values() for _, attr in cols
                   if attr not in ("PA", "Sack", "INT", "TD")})
    aggs = [func.sum(getattr(PlayerGameStats, c)).label(c) for c in cols]
    query = (
        select(Player.id, *aggs)
        .join(PlayerGameStats, PlayerGameStats.player_id == Player.id)
        .join(Game, Game.id == PlayerGameStats.game_id)
        .where(Game.season_year == year, Game.season_type == "regular")
        .group_by(Player.id)
    )
    out: dict[str, dict[str, int]] = {}
    for row in session.execute(query):
        out[f"p{row[0]}"] = {c: int(v or 0) for c, v in zip(cols, row[1:])}
    return out


def _team_offense(session: Session, year: int) -> dict[str, dict[str, int]]:
    """{team_abbr: {total_yards, plays, rush, pass, *_rank}} for the season."""
    from .models import Game, Team, TeamGameStats

    query = (
        select(
            Team.abbreviation,
            func.sum(TeamGameStats.total_yards), func.sum(TeamGameStats.plays),
            func.sum(TeamGameStats.rushing_yards), func.sum(TeamGameStats.passing_yards),
        )
        .join(TeamGameStats, TeamGameStats.team_id == Team.id)
        .join(Game, Game.id == TeamGameStats.game_id)
        .where(Game.season_year == year, Game.season_type == "regular")
        .group_by(Team.id)
    )
    data: dict[str, dict[str, int]] = {}
    for abbr, ty, pl, ru, pa in session.execute(query):
        data[abbr] = {"total_yards": int(ty or 0), "plays": int(pl or 0),
                      "rush": int(ru or 0), "pass": int(pa or 0)}
    for key in ("total_yards", "plays", "rush", "pass"):
        for rank, abbr in enumerate(sorted(data, key=lambda t: data[t][key], reverse=True), 1):
            data[abbr][key + "_rank"] = rank
    return data


def _dst_last_year(session: Session, year: int) -> dict[str, dict[str, float]]:
    """{d<abbr>: {PA (per game), Sack, INT, TD}} from team defensive stats."""
    from .models import Game, Team, TeamGameStats

    query = (
        select(
            Team.abbreviation, func.count(TeamGameStats.id),
            func.sum(TeamGameStats.points_allowed), func.sum(TeamGameStats.sacks),
            func.sum(TeamGameStats.interceptions),
            func.sum(TeamGameStats.defensive_tds), func.sum(TeamGameStats.special_teams_tds),
        )
        .join(TeamGameStats, TeamGameStats.team_id == Team.id)
        .join(Game, Game.id == TeamGameStats.game_id)
        .where(Game.season_year == year, Game.season_type == "regular")
        .group_by(Team.id)
    )
    out: dict[str, dict[str, float]] = {}
    for abbr, g, pa, sk, inte, dtd, sttd in session.execute(query):
        g = int(g or 0)
        out[f"d{abbr}"] = {
            "PA": round((pa or 0) / g, 1) if g else 0.0, "Sack": int(sk or 0),
            "INT": int(inte or 0), "TD": int((dtd or 0) + (sttd or 0)),
        }
    return out


def _format_statline(key: str, pos: str, pstats: dict, dstats: dict) -> str:
    """Compact last-year stat line for a player/defense (position-aware)."""
    if pos == "DST":
        d = dstats.get(key, {})
        return f"{d.get('PA', 0)}PA/g {d.get('Sack', 0)}sk {d.get('INT', 0)}int {d.get('TD', 0)}td"
    s = pstats.get(key)
    if not s:
        return ""
    if pos == "QB":
        return (f"{s['pass_yards']}yd {s['pass_touchdowns']}td {s['interceptions_thrown']}int, "
                f"{s['rush_attempts']}car {s['rush_yards']}ryd")
    if pos == "K":
        return f"{s['field_goals_made']}/{s['field_goals_attempted']}FG {s['extra_points_made']}XP"
    return (f"{s['receptions']}rec {s['receiving_yards']}yd {s['receiving_touchdowns']}td, "
            f"{s['rush_attempts']}car {s['rush_yards']}ryd {s['rush_touchdowns']}rtd")


def _format_team_context(team: str, pos: str, toff: dict) -> str:
    """Team-offense rank summary for offensive players (blank otherwise)."""
    if pos not in TEAM_OFFENSE_POSITIONS:
        return ""
    o = toff.get(team)
    if not o:
        return ""
    return f"off yds#{o['total_yards_rank']} pass#{o['pass_rank']} rush#{o['rush_rank']}"


def write_tiers_csv(
    session: Session,
    path: str,
    *,
    tiers: dict[str, int],
    prices: dict[str, float] | None = None,
    year: int | None = None,
    config: LeagueConfig = DEFAULT_LEAGUE,
    rules: ScoringRules = DEFAULT_RULES,
    basis: str = "w3yr",
) -> str:
    """Write an enriched tiers CSV (names + last-year stats + prices) for editing."""
    import csv

    prices = prices or {}
    last_year = year or _latest_season(session)
    values = compute_values(session, year=year, config=config, rules=rules, basis=basis)
    by_key = {r.key: r for rows in values.values() for r in rows}
    pstats = _player_last_year(session, last_year) if last_year else {}
    toff = _team_offense(session, last_year) if last_year else {}
    dstats = _dst_last_year(session, last_year) if last_year else {}

    with open(path, "w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["key", "manual_tier", "name", "pos", "total", "ppg",
                         "lastyr_stats", "team_offense", "price"])
        for key, tier in tiers.items():
            r = by_key.get(key)
            pos = r.position if r else ""
            writer.writerow([
                key, tier, r.name if r else key, pos,
                r.total if r else "", r.ppg if r else "",
                _format_statline(key, pos, pstats, dstats),
                _format_team_context(r.team if r else "", pos, toff),
                prices.get(key, ""),
            ])
    return path


def write_cheatsheet(
    session: Session,
    path: str,
    *,
    year: int | None = None,
    config: LeagueConfig = DEFAULT_LEAGUE,
    rules: ScoringRules = DEFAULT_RULES,
    basis: str = "w3yr",
    manual_tiers: dict[str, int] | None = None,
    fixed_prices: dict[str, float] | None = None,
) -> str:
    """Write the tiered draft board to an .xlsx file. Returns the path.

    Produces a live **Draft Board** sheet (mark a player drafted + enter what
    they went for, and the remaining players' recommended prices re-adjust for
    auction inflation) plus static per-position tier sheets for reference.
    """
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    board = build_board(
        session, year=year, config=config, rules=rules, basis=basis,
        manual_tiers=manual_tiers, fixed_prices=fixed_prices,
    )
    last_year = year or _latest_season(session)
    player_stats = _player_last_year(session, last_year) if last_year else {}
    team_off = _team_offense(session, last_year) if last_year else {}
    dst_stats = _dst_last_year(session, last_year) if last_year else {}

    header_font = Font(bold=True)
    center = Alignment(horizontal="center")

    wb = Workbook()
    wb.remove(wb.active)

    def _tier_fill(tier: int) -> PatternFill:
        return PatternFill("solid", fgColor=_TIER_FILLS[(tier - 1) % len(_TIER_FILLS)])

    # --- Static per-position tier sheets, with last-year stats inline ------
    def _position_sheet(pos: str, rows: list[BoardRow]) -> None:
        ws = wb.create_sheet(pos[:31])
        stat_cols = STAT_COLS.get(pos, [])
        show_team = pos in TEAM_OFFENSE_POSITIONS
        headers = (["Tier", "Rk", "Player", "Tm", "$", "PPG"]
                   + [h for h, _ in stat_cols]
                   + ([h for h, _ in _OFFENSE_METRICS] if show_team else []))
        ws.append(headers)
        for c in range(1, len(headers) + 1):
            ws.cell(row=1, column=c).font = header_font
            ws.cell(row=1, column=c).alignment = center

        for r in rows:
            name = r.name + (" (R)" if r.is_rookie else "")
            line: list = [r.tier, r.pos_rank, name, r.team, r.dollars, r.ppg]
            src = dst_stats.get(r.key, {}) if pos == "DST" else player_stats.get(r.key, {})
            for _h, attr in stat_cols:
                line.append(src.get(attr, 0))
            if show_team:
                off = team_off.get(r.team, {})
                for _h, key in _OFFENSE_METRICS:
                    val, rk = off.get(key), off.get(key + "_rank")
                    line.append(f"{val} (#{rk})" if val else "")
            ws.append(line)
            for c in range(1, len(headers) + 1):
                ws.cell(row=ws.max_row, column=c).fill = _tier_fill(r.tier)

        ws.freeze_panes = "C2"
        for c in range(1, len(headers) + 1):
            ws.column_dimensions[get_column_letter(c)].width = 20 if c == 3 else 8
        for row in range(2, ws.max_row + 1):
            ws.cell(row=row, column=5).number_format = '"$"0'  # $ column

    for pos in ALL_POSITIONS:
        if pos in board:
            _position_sheet(pos, board[pos])

    # --- Coaching reference -------------------------------------------------
    from .models import Team

    teams = sorted(
        (t for t in session.scalars(select(Team)) if t.head_coach or t.offensive_coordinator),
        key=lambda t: t.abbreviation,
    )
    if teams:
        ws = wb.create_sheet("Coaching")
        ws.append(["Team", "Head Coach", "Off. Coordinator", "Play-caller"])
        for c in range(1, 5):
            ws.cell(row=1, column=c).font = header_font
        for t in teams:
            ws.append([
                t.abbreviation, t.head_coach or "TBD",
                t.offensive_coordinator or "TBD", t.play_caller or "TBD",
            ])
        ws.freeze_panes = "A2"
        for c, w in zip("ABCD", (8, 20, 20, 20)):
            ws.column_dimensions[c].width = w

    # --- Live Draft Board: recommended prices that react to picks ----------
    _draft_sheet(wb, board, config, header_font, center, _tier_fill)
    wb.move_sheet("Draft Board", -(len(wb.sheetnames) - 1))  # make it first

    wb.save(path)
    return path


# Draft Board column layout (1-indexed):
#  A Pos  B Tier  C Player  D Base$  E Rec$  F Drafted  G Paid  H Weight(hidden)
#  I UserRtg  J LastYr  K PPG  L 3yr  M Tm  N Ovr ; control block in P/Q.
_DRAFT_HEADERS = ["Pos", "Tier", "Player", "Base$", "Rec$", "Drafted", "Paid",
                  "Weight", "UserRtg", "LastYr", "PPG", "3yrWtd", "Tm", "Ovr"]


def _draft_sheet(wb, board, config, header_font, center, tier_fill) -> None:
    ws = wb.create_sheet("Draft Board")
    ws.append(_DRAFT_HEADERS)
    for c in range(1, len(_DRAFT_HEADERS) + 1):
        ws.cell(row=1, column=c).font = header_font
        ws.cell(row=1, column=c).alignment = center

    rows = sorted(
        (r for rows in board.values() for r in rows),
        key=lambda r: r.dollars, reverse=True,
    )
    last = len(rows) + 1  # last data row (header is row 1)

    drafted = f"$F$2:$F${last}"
    paid = f"$G$2:$G${last}"
    weight = f"$H$2:$H${last}"

    for i, r in enumerate(rows, start=2):
        name = r.name + (" (R)" if r.is_rookie else "")
        ws.append([
            r.position, r.tier, name, r.dollars, None, None, None, None,
            r.user_rating, r.total, r.ppg, r.w3yr, r.team, r.overall_rank,
        ])
        # Weight = max(Base$ - 1, 0); recompute defensively in-sheet.
        ws.cell(row=i, column=8).value = f"=MAX(D{i}-1,0)"
        # Rec$ = paid if drafted, else inflation-adjusted allocation of the
        # remaining pool across remaining weights (control block in column Q).
        ws.cell(row=i, column=5).value = (
            f'=IF(F{i}="x",G{i},'
            f'IF($Q$7>0,ROUND(1+H{i}/$Q$7*($Q$5-$Q$6),0),D{i}))'
        )
        for c in range(1, len(_DRAFT_HEADERS) + 1):
            ws.cell(row=i, column=c).fill = tier_fill(r.tier)

    # Control block (labels in P, live values in Q).
    controls = [
        ("Total pool", config.pool),
        ("Total slots", config.teams * config.roster_size),
        ("Spent", f'=SUMIFS({paid},{drafted},"x")'),
        ("Drafted", f'=COUNTIF({drafted},"x")'),
        ("Remaining pool", "=Q1-Q3"),
        ("Remaining slots", "=Q2-Q4"),
        ("Remaining weight", f'=SUMIFS({weight},{drafted},"<>x")'),
    ]
    for idx, (label, value) in enumerate(controls, start=1):
        ws.cell(row=idx, column=16, value=label).font = header_font
        ws.cell(row=idx, column=17, value=value)

    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:N{last}"
    ws.column_dimensions["C"].width = 22
    for col in ("A", "B", "D", "E", "F", "G", "I", "J", "K", "L", "M", "N"):
        ws.column_dimensions[col].width = 9
    ws.column_dimensions["H"].hidden = True
    ws.column_dimensions["P"].width = 16
    for row in range(2, last + 1):
        for col in (4, 5, 7):  # Base$, Rec$, Paid
            ws.cell(row=row, column=col).number_format = '"$"0'
    ws.cell(row=1, column=17).number_format = '"$"0'  # total pool
    ws.cell(row=5, column=17).number_format = '"$"0'  # remaining pool
