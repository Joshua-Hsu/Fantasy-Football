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

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import UserRating
from .ratings import seed_ratings, user_rating_tiers
from .scoring import DEFAULT_RULES, ScoringRules
from .valuation import ALL_POSITIONS, DEFAULT_LEAGUE, LeagueConfig, compute_values

# A few soft fills cycled per tier so tier bands are easy to scan.
_TIER_FILLS = ["FFF6E5", "E8F0FE", "E9F7EF", "FCE8F3", "F3E8FD", "EAF2F8", "FFF0E6"]

_HEADERS = ["Tier", "PosRank", "TierRank", "Player", "Tm", "Ovr", "$", "UserRtg",
            "LastYr", "PPG", "3yrWtd"]


class BoardRow(NamedTuple):
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

    Tiers come from the head-to-head user ratings. ``manual_tiers`` (hand-set
    positional tiers) seed those ratings as a *starting point* — picks still move
    them — rather than hard-locking the tiers. ``fixed_prices`` pins expected
    market prices for specific players; the field re-prices around them.
    """
    seed_ratings(session, year=year, config=config, rules=rules, basis=basis,
                 manual_tiers=manual_tiers)
    tiers = user_rating_tiers(session)
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
                    tier=r.tier, pos_rank=i, tier_rank=per_tier_seen[r.tier],
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
    header_font = Font(bold=True)
    center = Alignment(horizontal="center")

    wb = Workbook()
    wb.remove(wb.active)

    def _tier_fill(tier: int) -> PatternFill:
        return PatternFill("solid", fgColor=_TIER_FILLS[(tier - 1) % len(_TIER_FILLS)])

    # --- Static per-position tier sheets (pre-draft reference) --------------
    def _position_sheet(title: str, rows: list[BoardRow]) -> None:
        ws = wb.create_sheet(title[:31])
        ws.append(_HEADERS)
        for c in range(1, len(_HEADERS) + 1):
            ws.cell(row=1, column=c).font = header_font
            ws.cell(row=1, column=c).alignment = center
        for r in rows:
            name = r.name + (" (R)" if r.is_rookie else "")
            ws.append([r.tier, r.pos_rank, r.tier_rank, name, r.team, r.overall_rank,
                       r.dollars, r.user_rating, r.total, r.ppg, r.w3yr])
            for c in range(1, len(_HEADERS) + 1):
                ws.cell(row=ws.max_row, column=c).fill = _tier_fill(r.tier)
        ws.freeze_panes = "A2"
        for c in range(1, len(_HEADERS) + 1):
            ws.column_dimensions[get_column_letter(c)].width = 22 if c == 4 else 9
        for row in range(2, ws.max_row + 1):
            ws.cell(row=row, column=7).number_format = '"$"0'  # $ column

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
