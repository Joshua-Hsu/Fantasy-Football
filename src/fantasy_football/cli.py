"""Command-line entry point for database administration.

Usage::

    python -m fantasy_football.cli init-db          # create tables
    python -m fantasy_football.cli init-db --echo   # ... with SQL logging
    python -m fantasy_football.cli info             # show DB location & tables
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

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
