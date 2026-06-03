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

_HEADERS = ["Tier", "PosRank", "TierRank", "Player", "$", "UserRtg",
            "LastYr", "PPG", "3yrWtd"]


class BoardRow(NamedTuple):
    tier: int
    pos_rank: int
    tier_rank: int
    name: str
    dollars: float
    user_rating: float
    total: float
    ppg: float
    w3yr: float
    position: str


def build_board(
    session: Session,
    *,
    year: int | None = None,
    config: LeagueConfig = DEFAULT_LEAGUE,
    rules: ScoringRules = DEFAULT_RULES,
    basis: str = "w3yr",
) -> dict[str, list[BoardRow]]:
    """Per-position rows ordered by user rating, with tier and within-tier rank.

    Ratings are seeded if missing, so this works before any picks (tiers then
    equal the initial k-means tiers).
    """
    seed_ratings(session, year=year, config=config, rules=rules, basis=basis)
    tiers = user_rating_tiers(session)
    values = compute_values(
        session, year=year, config=config, rules=rules, basis=basis, manual_tiers=tiers
    )
    ratings = {r.key: r.rating for r in session.scalars(select(UserRating))}

    board: dict[str, list[BoardRow]] = {}
    for pos in ALL_POSITIONS:
        rows = values.get(pos, [])
        if not rows:
            continue
        ranked = sorted(rows, key=lambda r: ratings.get(r.key, r.basis_value), reverse=True)
        per_tier_seen: dict[int, int] = {}
        out: list[BoardRow] = []
        for i, r in enumerate(ranked, 1):
            per_tier_seen[r.tier] = per_tier_seen.get(r.tier, 0) + 1
            out.append(
                BoardRow(
                    tier=r.tier, pos_rank=i, tier_rank=per_tier_seen[r.tier],
                    name=r.name, dollars=r.dollars,
                    user_rating=round(ratings.get(r.key, r.basis_value), 1),
                    total=r.total, ppg=r.ppg, w3yr=r.w3yr, position=pos,
                )
            )
        board[pos] = out
    return board


def write_cheatsheet(
    session: Session,
    path: str,
    *,
    year: int | None = None,
    config: LeagueConfig = DEFAULT_LEAGUE,
    rules: ScoringRules = DEFAULT_RULES,
    basis: str = "w3yr",
) -> str:
    """Write the tiered draft board to an .xlsx file. Returns the path.

    Produces a live **Draft Board** sheet (mark a player drafted + enter what
    they went for, and the remaining players' recommended prices re-adjust for
    auction inflation) plus static per-position tier sheets for reference.
    """
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    board = build_board(session, year=year, config=config, rules=rules, basis=basis)
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
            ws.append([r.tier, r.pos_rank, r.tier_rank, r.name, r.dollars,
                       r.user_rating, r.total, r.ppg, r.w3yr])
            for c in range(1, len(_HEADERS) + 1):
                ws.cell(row=ws.max_row, column=c).fill = _tier_fill(r.tier)
        ws.freeze_panes = "A2"
        for c in range(1, len(_HEADERS) + 1):
            ws.column_dimensions[get_column_letter(c)].width = 22 if c == 4 else 9
        for row in range(2, ws.max_row + 1):
            ws.cell(row=row, column=5).number_format = '"$"0'

    for pos in ALL_POSITIONS:
        if pos in board:
            _position_sheet(pos, board[pos])

    # --- Live Draft Board: recommended prices that react to picks ----------
    _draft_sheet(wb, board, config, header_font, center, _tier_fill)
    wb.move_sheet("Draft Board", -(len(wb.sheetnames) - 1))  # make it first

    wb.save(path)
    return path


# Draft Board column layout (1-indexed):
#  A Pos  B Tier  C Player  D Base$  E Rec$  F Drafted  G Paid  H Weight(hidden)
#  I UserRtg  J LastYr  K PPG  L 3yr  ; control block in N (labels) / O (values)
_DRAFT_HEADERS = ["Pos", "Tier", "Player", "Base$", "Rec$", "Drafted", "Paid",
                  "Weight", "UserRtg", "LastYr", "PPG", "3yrWtd"]


def _draft_sheet(wb, board, config, header_font, center, tier_fill) -> None:
    from openpyxl.utils import get_column_letter

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
        ws.append([
            r.position, r.tier, r.name, r.dollars, None, None, None, None,
            r.user_rating, r.total, r.ppg, r.w3yr,
        ])
        # Weight = max(Base$ - 1, 0); recompute defensively in-sheet.
        ws.cell(row=i, column=8).value = f"=MAX(D{i}-1,0)"
        # Rec$ = paid if drafted, else inflation-adjusted allocation of the
        # remaining pool across remaining weights.
        ws.cell(row=i, column=5).value = (
            f'=IF(F{i}="x",G{i},'
            f'IF($O$7>0,ROUND(1+H{i}/$O$7*($O$5-$O$6),0),D{i}))'
        )
        for c in range(1, len(_DRAFT_HEADERS) + 1):
            ws.cell(row=i, column=c).fill = tier_fill(r.tier)

    # Control block (labels in N, live values in O).
    controls = [
        ("Total pool", config.pool),
        ("Total slots", config.teams * config.roster_size),
        ("Spent", f'=SUMIFS({paid},{drafted},"x")'),
        ("Drafted", f'=COUNTIF({drafted},"x")'),
        ("Remaining pool", "=O1-O3"),
        ("Remaining slots", "=O2-O4"),
        ("Remaining weight", f'=SUMIFS({weight},{drafted},"<>x")'),
    ]
    for idx, (label, value) in enumerate(controls, start=1):
        ws.cell(row=idx, column=14, value=label).font = header_font
        ws.cell(row=idx, column=15, value=value)

    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:L{last}"
    ws.column_dimensions["C"].width = 22
    for col in ("A", "B", "D", "E", "F", "G", "I", "J", "K", "L"):
        ws.column_dimensions[col].width = 9
    ws.column_dimensions["H"].hidden = True
    ws.column_dimensions["N"].width = 16
    for row in range(2, last + 1):
        ws.cell(row=row, column=4).number_format = '"$"0'
        ws.cell(row=row, column=5).number_format = '"$"0'
        ws.cell(row=row, column=7).number_format = '"$"0'
    ws.cell(row=1, column=15).number_format = '"$"0'
    ws.cell(row=5, column=15).number_format = '"$"0'
