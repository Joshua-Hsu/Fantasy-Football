"""Command-line entry point for database administration.

Usage::

    python -m fantasy_football.cli init-db          # create tables
    python -m fantasy_football.cli init-db --echo   # ... with SQL logging
    python -m fantasy_football.cli info             # show DB location & tables
    python -m fantasy_football.cli load-teams       # load all franchises
    python -m fantasy_football.cli load-season --year 2024
    python -m fantasy_football.cli load-seasons --start 2020   # ...through latest
    python -m fantasy_football.cli leaders --year 2024 --scoring half_ppr --position RB
    python -m fantasy_football.cli values  --year 2025 --position RB
    python -m fantasy_football.cli serve                # head-to-head tier game
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


def _read_manual_tiers(path: str | None) -> dict[str, int]:
    """Read manual tier overrides from a CSV with columns ``key,manual_tier``."""
    if not path:
        return {}
    import csv

    tiers: dict[str, int] = {}
    with open(path, newline="") as fh:
        for row in csv.DictReader(fh):
            key = (row.get("key") or "").strip()
            value = (row.get("manual_tier") or "").strip()
            if key and value:
                tiers[key] = int(value)
    return tiers


def _cmd_values(args: argparse.Namespace) -> int:
    import csv

    from .scoring import PRESETS
    from .valuation import ALL_POSITIONS, LeagueConfig, compute_values

    config = LeagueConfig(teams=args.teams, budget=args.budget)
    manual_tiers = _read_manual_tiers(args.tiers_file)

    with _open_session(args) as session:
        if args.use_user_ratings:
            from .ratings import user_rating_tiers

            manual_tiers = {**user_rating_tiers(session), **manual_tiers}
        values = compute_values(
            session,
            year=args.year,
            config=config,
            rules=PRESETS[args.scoring],
            basis=args.basis,
            manual_tiers=manual_tiers,
            active_only=False if args.all_players else None,
        )

    positions = [args.position.upper()] if args.position else list(ALL_POSITIONS)
    note = " (tier = manual override)" if manual_tiers else ""
    if args.use_user_ratings:
        note = " (tier = user ratings)"
    for pos in positions:
        rows = values.get(pos, [])
        if not rows:
            continue
        print(f"\n=== {pos} - {args.basis} basis, {config.teams}tm/${config.budget}{note} ===")
        print(
            f"{'#':>3}  {'Player':<22} {'Tm':<3} {'Ovr':>4} {'Total':>6} {'PPG':>5} "
            f"{'3yr':>6} {'Tier':>4} {'VOR':>6} {'$':>5}"
        )
        for i, r in enumerate(rows[: args.limit], 1):
            tier = f"{r.tier}" if not manual_tiers else f"{r.tier}*"
            name = (r.name + (" (R)" if r.is_rookie else ""))[:22]
            print(
                f"{i:>3}  {name:<22} {(r.team or ''):<3} {r.overall_rank:>4} "
                f"{r.total:>6.1f} {r.ppg:>5.1f} {r.w3yr:>6.1f} {tier:>4} "
                f"{r.vor:>6.1f} {r.dollars:>5.0f}"
            )

    if args.export:
        with open(args.export, "w", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(
                ["key", "name", "position", "games", "total", "ppg", "w3yr",
                 "kmeans_tier", "vor", "dollars", "manual_tier"]
            )
            for pos in ALL_POSITIONS:
                for r in values.get(pos, []):
                    writer.writerow(
                        [r.key, r.name, r.position, r.games, r.total, r.ppg, r.w3yr,
                         r.kmeans_tier, r.vor, r.dollars, ""]
                    )
        print(f"\nExported full table to {args.export} "
              f"(edit the manual_tier column, then re-run with --tiers-file).")
    return 0


def _cmd_load_active(args: argparse.Namespace) -> int:
    import datetime as _dt

    from .ingest import load_active_roster

    year = args.year or _dt.date.today().year
    with _open_session(args) as session:
        n = load_active_roster(session, year)
    print(f"Marked {n} active players from the {year} roster")
    return 0


def _cmd_load_coaching(args: argparse.Namespace) -> int:
    from .ingest import load_coaching

    with _open_session(args) as session:
        n = load_coaching(session, args.file)
    print(f"Loaded coaching staff for {n} teams")
    return 0


def _cmd_coaching_template(args: argparse.Namespace) -> int:
    import csv

    from sqlalchemy import select

    from .ingest import latest_head_coaches
    from .models import Team

    coaches = latest_head_coaches()
    with _open_session(args) as session:
        known = {t.abbreviation for t in session.scalars(select(Team))}
    teams = sorted(known & set(coaches)) or sorted(known)
    with open(args.out, "w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["team", "head_coach", "offensive_coordinator", "play_caller"])
        for team in teams:
            writer.writerow([team, coaches.get(team, ""), "", ""])
    print(f"Wrote coaching template ({len(teams)} teams) to {args.out} "
          f"- fill OC/play-caller, then load-coaching --file {args.out}")
    return 0


def _cmd_cheatsheet(args: argparse.Namespace) -> int:
    from .export import write_cheatsheet
    from .scoring import PRESETS
    from .valuation import LeagueConfig

    config = LeagueConfig(teams=args.teams, budget=args.budget)
    with _open_session(args) as session:
        path = write_cheatsheet(
            session, args.out, year=args.year, config=config,
            rules=PRESETS[args.scoring], basis=args.basis,
        )
    print(f"Wrote tiered draft board to {path}")
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    from .web import create_app

    app = create_app(args.db)
    print(f"Serving comparison game at http://{args.host}:{args.port}  (Ctrl-C to stop)")
    app.run(host=args.host, port=args.port, debug=False)
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

    p_values = sub.add_parser(
        "values", help="Compute tiers and auction values from historical scoring"
    )
    p_values.add_argument("--year", type=int, default=None, help="Most recent season (default: latest loaded)")
    p_values.add_argument(
        "--basis", choices=["total", "ppg", "w3yr"], default="w3yr",
        help="Value used for pricing; all three are shown (default: w3yr)",
    )
    p_values.add_argument(
        "--scoring", choices=["standard", "half_ppr", "ppr"], default="half_ppr",
        help="Scoring system (default: half_ppr)",
    )
    p_values.add_argument("--position", default=None, help="Limit to one position")
    p_values.add_argument("--limit", type=int, default=20, help="Rows per position (default 20)")
    p_values.add_argument("--teams", type=int, default=12, help="League size (default 12)")
    p_values.add_argument("--budget", type=int, default=200, help="Auction budget (default 200)")
    p_values.add_argument(
        "--tiers-file", default=None, dest="tiers_file",
        help="CSV of manual tier overrides (columns: key,manual_tier)",
    )
    p_values.add_argument(
        "--export", default=None,
        help="Write the full table to this CSV for manual tier editing",
    )
    p_values.add_argument(
        "--use-user-ratings", action="store_true", dest="use_user_ratings",
        help="Draw tiers from the head-to-head user ratings",
    )
    p_values.add_argument(
        "--all-players", action="store_true", dest="all_players",
        help="Don't restrict to active rosters (include anyone with recent stats)",
    )
    p_values.set_defaults(func=_cmd_values)

    p_active = sub.add_parser("load-active", help="Mark the active draft pool from a season roster")
    p_active.add_argument("--year", type=int, default=None, help="Roster year (default: current year)")
    p_active.set_defaults(func=_cmd_load_active)

    p_coachtpl = sub.add_parser("coaching-template", help="Write a coaching CSV to fill in")
    p_coachtpl.add_argument("--out", default="coaching.csv", help="Output CSV path")
    p_coachtpl.set_defaults(func=_cmd_coaching_template)

    p_coach = sub.add_parser("load-coaching", help="Load team coaching staff from a CSV")
    p_coach.add_argument("--file", required=True, help="CSV: team,head_coach,offensive_coordinator,play_caller")
    p_coach.set_defaults(func=_cmd_load_coaching)

    p_serve = sub.add_parser("serve", help="Run the head-to-head comparison web app")
    p_serve.add_argument("--host", default="127.0.0.1", help="Bind host (use 0.0.0.0 for LAN/phone)")
    p_serve.add_argument("--port", type=int, default=8000, help="Port (default 8000)")
    p_serve.set_defaults(func=_cmd_serve)

    p_sheet = sub.add_parser("cheatsheet", help="Export a tiered draft board (.xlsx)")
    p_sheet.add_argument("--out", default="draft_board.xlsx", help="Output .xlsx path")
    p_sheet.add_argument("--year", type=int, default=None, help="Most recent season (default: latest)")
    p_sheet.add_argument("--basis", choices=["total", "ppg", "w3yr"], default="w3yr")
    p_sheet.add_argument("--scoring", choices=["standard", "half_ppr", "ppr"], default="half_ppr")
    p_sheet.add_argument("--teams", type=int, default=12)
    p_sheet.add_argument("--budget", type=int, default=200)
    p_sheet.set_defaults(func=_cmd_cheatsheet)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
