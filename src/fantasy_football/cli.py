"""Command-line entry point for database administration.

Usage::

    python -m fantasy_football.cli init-db          # create tables
    python -m fantasy_football.cli init-db --echo   # ... with SQL logging
    python -m fantasy_football.cli info             # show DB location & tables
    python -m fantasy_football.cli load-teams       # load all franchises
    python -m fantasy_football.cli load-season --year 2024
    python -m fantasy_football.cli load-seasons --start 2020   # ...through latest
    python -m fantasy_football.cli leaders --year 2024 --scoring half_ppr --position RB
    python -m fantasy_football.cli leaders --year 2024 --position DST
"""

from __future__ import annotations

import argparse
import sys

from sqlalchemy import inspect

from .db import create_db_engine, database_url, init_db


def _cmd_init_db(args: argparse.Namespace) -> int:
    engine = create_db_engine(args.db, echo=args.echo)
    init_db(engine)
    print(f"Initialized database at {database_url(args.db)}")
    tables = sorted(inspect(engine).get_table_names())
    print(f"Tables: {', '.join(tables) if tables else '(none)'}")
    return 0


def _cmd_info(args: argparse.Namespace) -> int:
    engine = create_db_engine(args.db)
    print(f"Database URL: {database_url(args.db)}")
    tables = sorted(inspect(engine).get_table_names())
    print(f"Tables ({len(tables)}): {', '.join(tables) if tables else '(none)'}")
    return 0


def _open_session(args: argparse.Namespace):
    """Create the engine, ensure tables exist, and return a live session."""
    from .db import create_db_engine, get_sessionmaker, init_db

    engine = create_db_engine(args.db, echo=getattr(args, "echo", False))
    init_db(engine)  # ingest should work against a fresh DB too
    return get_sessionmaker(engine)()


def _cmd_load_teams(args: argparse.Namespace) -> int:
    from .ingest import load_teams

    with _open_session(args) as session:
        n = load_teams(session)
    print(f"Loaded {n} teams")
    return 0


def _cmd_load_season(args: argparse.Namespace) -> int:
    from .ingest import load_season

    with _open_session(args) as session:
        summary = load_season(session, args.year)
    print(
        f"Season {args.year}: "
        + ", ".join(f"{k}={v}" for k, v in summary.items())
    )
    return 0


def _cmd_load_seasons(args: argparse.Namespace) -> int:
    from .ingest import load_seasons

    with _open_session(args) as session:
        results = load_seasons(session, args.start, args.end)
    if not results:
        print("No seasons with available data in range.")
        return 0
    for year, summary in results.items():
        print(f"  {year}: " + ", ".join(f"{k}={v}" for k, v in summary.items()))
    totals = {
        k: sum(s[k] for s in results.values())
        for k in next(iter(results.values()))
    }
    span = f"{min(results)}-{max(results)}"
    print(f"Loaded {len(results)} seasons ({span}): "
          + ", ".join(f"{k}={v}" for k, v in totals.items()))
    return 0


def _cmd_leaders(args: argparse.Namespace) -> int:
    from .scoring import PRESETS, season_leaders, team_defense_season_leaders

    rules = PRESETS[args.scoring]
    season_type = None if args.season_type == "all" else args.season_type
    is_dst = (args.position or "").upper() == "DST"

    with _open_session(args) as session:
        if is_dst:
            rows = team_defense_season_leaders(
                session, args.year, season_type=season_type, limit=args.limit
            )
        else:
            rows = season_leaders(
                session,
                args.year,
                rules,
                season_type=season_type,
                position=args.position,
                limit=args.limit,
            )

    scope = "DST" if is_dst else (args.position or "ALL")
    print(
        f"{args.year} {args.scoring} leaders "
        f"({scope}, {args.season_type}) - top {args.limit}"
    )
    print(f"{'#':>3}  {'Player':<26} {'Pos':<4} {'G':>3} {'Pts':>8} {'PPG':>6}")
    for i, r in enumerate(rows, 1):
        print(
            f"{i:>3}  {(r.player or '')[:26]:<26} {(r.position or ''):<4} "
            f"{r.games:>3} {r.points:>8.2f} {r.points_per_game:>6.2f}"
        )
    if not rows:
        print("(no data - has this season been loaded?)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fantasy_football", description=__doc__)
    parser.add_argument(
        "--db",
        default=None,
        help="Database path (defaults to $FANTASY_FOOTBALL_DB or data/fantasy_football.db)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init-db", help="Create all database tables")
    p_init.add_argument("--echo", action="store_true", help="Log SQL statements")
    p_init.set_defaults(func=_cmd_init_db)

    p_info = sub.add_parser("info", help="Show database location and tables")
    p_info.set_defaults(func=_cmd_info)

    p_teams = sub.add_parser("load-teams", help="Load all franchises from nflverse")
    p_teams.set_defaults(func=_cmd_load_teams)

    p_season = sub.add_parser("load-season", help="Load one season from nflverse")
    p_season.add_argument("--year", type=int, required=True, help="Season year, e.g. 2024")
    p_season.set_defaults(func=_cmd_load_season)

    p_seasons = sub.add_parser(
        "load-seasons", help="Load a range of seasons (skips unplayed years)"
    )
    p_seasons.add_argument("--start", type=int, default=2020, help="First season (default 2020)")
    p_seasons.add_argument(
        "--end", type=int, default=None, help="Last season (default: current year)"
    )
    p_seasons.set_defaults(func=_cmd_load_seasons)

    p_leaders = sub.add_parser("leaders", help="Show fantasy scoring leaders for a season")
    p_leaders.add_argument("--year", type=int, required=True, help="Season year, e.g. 2024")
    p_leaders.add_argument(
        "--scoring", choices=["standard", "half_ppr", "ppr"], default="half_ppr",
        help="Scoring system (default: half_ppr)",
    )
    p_leaders.add_argument(
        "--position", default=None,
        help="Filter to a position (QB/RB/WR/TE/K, or DST for team defenses)",
    )
    p_leaders.add_argument("--limit", type=int, default=25, help="Number of rows (default 25)")
    p_leaders.add_argument(
        "--season-type", choices=["regular", "postseason", "all"], default="regular",
        dest="season_type", help="Which games to include (default: regular)",
    )
    p_leaders.set_defaults(func=_cmd_leaders)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
