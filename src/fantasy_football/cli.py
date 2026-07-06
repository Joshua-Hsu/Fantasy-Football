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
import re
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


# A valid entity key is "p<player_id>" or "d<team_abbr>" — nothing else is
# accepted from a (possibly hand-edited / untrusted) tiers CSV.
_KEY_RE = re.compile(r"^[pd][A-Za-z0-9_.\-]{1,32}$")
_MAX_TIERS_ROWS = 20000  # guard against absurdly large input files


def _read_manual_tiers(path: str | None) -> dict[str, int]:
    """Read manual tier overrides from a CSV with columns ``key,manual_tier``.

    Untrusted-input hardened: only well-formed keys are accepted, tiers are
    parsed leniently and clamped to 1..30, and bad rows are skipped rather than
    raising.
    """
    import csv
    import os

    if not path or not os.path.exists(path):
        return {}
    tiers: dict[str, int] = {}
    with open(path, newline="") as fh:
        for i, row in enumerate(csv.DictReader(fh)):
            if i >= _MAX_TIERS_ROWS:
                break
            key = (row.get("key") or "").strip()
            if not _KEY_RE.match(key):
                continue
            try:
                tier = int(float((row.get("manual_tier") or "").strip()))
            except ValueError:
                continue
            tiers[key] = max(1, min(tier, 30))
    return tiers


def _read_fixed_prices(path: str | None) -> dict[str, float]:
    """Read expected/market prices from a CSV's optional ``price`` column.

    Same hardening: valid keys only, lenient float parsing, clamped to 0..10000.
    """
    import csv
    import os

    if not path or not os.path.exists(path):
        return {}
    prices: dict[str, float] = {}
    with open(path, newline="") as fh:
        reader = csv.DictReader(fh)
        if "price" not in (reader.fieldnames or []):
            return {}
        for i, row in enumerate(reader):
            if i >= _MAX_TIERS_ROWS:
                break
            key = (row.get("key") or "").strip()
            value = (row.get("price") or "").strip()
            if not _KEY_RE.match(key) or not value:
                continue
            try:
                prices[key] = max(0.0, min(float(value), 10000.0))
            except ValueError:
                continue
    return prices


def _read_ratings(path: str | None) -> dict[str, float]:
    """Read the continuous user ``rating`` column from a master/picks CSV.

    Same hardening as the other readers: valid keys only, lenient float parsing,
    clamped to a sane range, bad rows skipped. Returns {} if there's no column.
    """
    import csv
    import os

    if not path or not os.path.exists(path):
        return {}
    ratings: dict[str, float] = {}
    with open(path, newline="") as fh:
        reader = csv.DictReader(fh)
        if "rating" not in (reader.fieldnames or []):
            return {}
        for i, row in enumerate(reader):
            if i >= _MAX_TIERS_ROWS:
                break
            key = (row.get("key") or "").strip()
            value = (row.get("rating") or "").strip()
            if not _KEY_RE.match(key) or not value:
                continue
            try:
                ratings[key] = max(-10000.0, min(float(value), 10000.0))
            except ValueError:
                continue
    return ratings


def _league_config(args: argparse.Namespace):
    """Build the LeagueConfig from CLI args, including --price-cap overrides.

    ``--price-cap POS=N`` (repeatable) replaces the default cap for POS;
    ``POS=0`` removes it entirely.
    """
    from .valuation import LeagueConfig

    caps = dict(LeagueConfig().price_caps)
    for spec in getattr(args, "price_caps", None) or []:
        pos, _, num = spec.partition("=")
        pos = pos.strip().upper()
        try:
            value = float(num)
        except ValueError:
            raise SystemExit(f"bad --price-cap {spec!r}; expected POS=NUMBER")
        if value > 0:
            caps[pos] = value
        else:
            caps.pop(pos, None)
    return LeagueConfig(teams=args.teams, budget=args.budget, price_caps=caps)


def _read_tier_notes(path: str | None) -> dict[tuple[str, int], str]:
    """Read hand-written tier descriptions from a master CSV's ``tier_note``.

    Keyed by (position, tier). The note is stored on every row of its tier;
    the first non-empty one wins. Same hardening: lenient parsing, size caps,
    bad rows skipped, note length clamped.
    """
    import csv
    import os

    if not path or not os.path.exists(path):
        return {}
    notes: dict[tuple[str, int], str] = {}
    with open(path, newline="") as fh:
        reader = csv.DictReader(fh)
        fields = reader.fieldnames or []
        if not {"tier_note", "pos", "manual_tier"} <= set(fields):
            return {}
        for i, row in enumerate(reader):
            if i >= _MAX_TIERS_ROWS:
                break
            note = (row.get("tier_note") or "").strip()
            pos = (row.get("pos") or "").strip().upper()
            if not note or not pos:
                continue
            try:
                tier = int(float((row.get("manual_tier") or "").strip()))
            except ValueError:
                continue
            notes.setdefault((pos, tier), note[:200])
    return notes


def _read_comps(path: str | None) -> dict[str, int]:
    """Read per-player head-to-head comparison counts (``comps`` column).

    Same hardening as the other readers; returns {} when the column is absent
    (legacy pick files).
    """
    import csv
    import os

    if not path or not os.path.exists(path):
        return {}
    comps: dict[str, int] = {}
    with open(path, newline="") as fh:
        reader = csv.DictReader(fh)
        if "comps" not in (reader.fieldnames or []):
            return {}
        for i, row in enumerate(reader):
            if i >= _MAX_TIERS_ROWS:
                break
            key = (row.get("key") or "").strip()
            value = (row.get("comps") or "").strip()
            if not _KEY_RE.match(key) or not value:
                continue
            try:
                # Per-submission clamp: nobody gets to claim thousands of
                # comparisons to seize the confidence weight. Real sessions
                # rarely exceed a few dozen per player.
                comps[key] = max(0, min(int(float(value)), 40))
            except ValueError:
                continue
    return comps


def _merge_comps(paths: list[str]) -> dict[str, int]:
    """Total comparisons per player across every pick file (all users)."""
    total: dict[str, int] = {}
    for path in paths:
        for key, n in _read_comps(path).items():
            total[key] = total.get(key, 0) + n
    return total


def _merge_ratings(paths: list[str], base: dict[str, float]) -> dict[str, float]:
    """Merge the ``rating`` columns of several pick exports into one map.

    A player's new rating is the **median** of his rating across every pick
    file that includes him — one vote per submission, and robust for the
    public site: a single troll submission shifts the median far less than it
    would shift a mean. Players nobody compared keep their ``base``
    (previous-master) rating, so refinements carry forward week to week.
    """
    from statistics import median

    votes: dict[str, list[float]] = {}
    for path in paths:
        for key, rating in _read_ratings(path).items():
            votes.setdefault(key, []).append(rating)
    merged = dict(base)
    for key, vals in votes.items():
        merged[key] = float(median(vals))
    return merged


def _cmd_values(args: argparse.Namespace) -> int:
    import csv

    from .scoring import PRESETS
    from .valuation import ALL_POSITIONS, LeagueConfig, compute_values

    config = _league_config(args)
    manual_tiers = _read_manual_tiers(args.tiers_file)
    fixed_prices = _read_fixed_prices(args.tiers_file)

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
            fixed_prices=fixed_prices,
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


def _cmd_import_tiers(args: argparse.Namespace) -> int:
    """Rebuild the master tiers by merging one or more pick-game exports.

    Each ``--file`` is an app export carrying continuous ``rating`` (and
    ``comps``) columns. Ratings are averaged across the files (one vote each)
    and **confidence-blended** onto the previous master's ratings
    (``--prices-from``): a player's pull toward the league's pick-game view is
    ``comps / (comps + confidence)``, so a couple of picks nudge him and many
    picks dominate. Un-compared players carry forward; integer tiers re-derive
    from the blended ratings; prices are preserved. Falls back to a file's
    integer ``manual_tier`` if it has no ``rating`` column (legacy exports).
    """
    from .export import write_tiers_csv

    files = args.file if isinstance(args.file, list) else [args.file]
    base_ratings = _read_ratings(getattr(args, "prices_from", None))
    user_ratings = _merge_ratings(files, base={})   # picked players only
    comps = _merge_comps(files)
    ratings = _merge_ratings(files, base_ratings)   # legacy detection / counts
    # Legacy fallback: if no file carried a rating column, use the integer tiers.
    legacy_tiers: dict[str, int] = {}
    if not ratings:
        for path in files:
            legacy_tiers.update(_read_manual_tiers(path))

    # Preserve prices from the incoming files, an explicit --prices-from, then the
    # output (most specific wins).
    existing_prices: dict[str, float] = {}
    for path in files:
        existing_prices.update(_read_fixed_prices(path))
    existing_prices.update(_read_fixed_prices(getattr(args, "prices_from", None)))
    existing_prices.update(_read_fixed_prices(args.out))

    # Tier notes carry forward from the previous master (incoming pick files
    # don't have them). Note: tiers are re-derived, so if the boundaries move a
    # note may land on a shifted group — review them after a rebuild.
    notes = _read_tier_notes(getattr(args, "prices_from", None))

    with _open_session(args) as session:
        write_tiers_csv(
            session, args.out,
            ratings=(base_ratings or None) if ratings else None,
            user_ratings=user_ratings or None,
            comps=comps or None,
            confidence=getattr(args, "confidence", None) or 6,
            tiers=legacy_tiers or None,
            prices=existing_prices, notes=notes or None,
        )
    blended = sum(1 for k in user_ratings if comps.get(k))
    print(f"Merged {len(files)} pick file(s) -> {args.out}: "
          f"{len(user_ratings)} picked ({blended} confidence-blended; base carried: "
          f"{len(base_ratings)}), prices preserved: {len(existing_prices)}. "
          f"Rebuild the board/app to apply.")
    return 0


def _cmd_load_active(args: argparse.Namespace) -> int:
    import datetime as _dt

    from .ingest import load_active_roster

    year = args.year or _dt.date.today().year
    with _open_session(args) as session:
        n = load_active_roster(session, year)
    print(f"Marked {n} active players from the {year} roster")
    return 0


def _cmd_load_draft(args: argparse.Namespace) -> int:
    import datetime as _dt

    from .ingest import load_draft_rookies

    year = args.year or _dt.date.today().year
    with _open_session(args) as session:
        n = load_draft_rookies(session, year, max_round=args.max_round)
    print(f"Added {n} rookies (rounds 1-{args.max_round}) from the {year} draft")
    return 0


def _cmd_load_redzone(args: argparse.Namespace) -> int:
    from sqlalchemy import func, select

    from .ingest import load_redzone
    from .models import Game

    with _open_session(args) as session:
        year = args.year or session.scalar(
            select(func.max(Game.season_year)).where(Game.season_type == "regular")
        )
        if year is None:
            print("No seasons loaded.")
            return 1
        n = load_redzone(session, year)
    print(f"Set red-zone targets on {n} stat lines ({year})")
    return 0


def _cmd_load_byes(args: argparse.Namespace) -> int:
    import datetime as _dt

    from .ingest import load_byes

    year = args.year or _dt.date.today().year
    with _open_session(args) as session:
        n = load_byes(session, year)
    print(f"Set bye weeks for {n} teams ({year})")
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
        writer.writerow(["team", "head_coach", "offensive_coordinator", "hc_new", "oc_new"])
        for team in teams:
            writer.writerow([team, coaches.get(team, ""), "", "", ""])
    print(f"Wrote coaching template ({len(teams)} teams) to {args.out} "
          f"- fill OC, then load-coaching --file {args.out}")
    return 0


def _read_depth_overrides(path: str | None) -> dict[str, str]:
    """Read manual backup corrections: CSV with ``player,backup`` columns.

    Keyed by the starter's name (lowercased). Wins over the fetched depth
    chart, so you can hand-fix any slot you disagree with.
    """
    import csv
    import os

    if not path or not os.path.exists(path):
        return {}
    out: dict[str, str] = {}
    with open(path, newline="") as fh:
        reader = csv.DictReader(fh)
        if not {"player", "backup"} <= set(reader.fieldnames or []):
            return {}
        for i, row in enumerate(reader):
            if i >= _MAX_TIERS_ROWS:
                break
            player = (row.get("player") or "").strip()
            backup = (row.get("backup") or "").strip()
            if player and backup:
                out[player.lower()] = backup[:64]
    return out


def _cmd_cheatsheet(args: argparse.Namespace) -> int:
    import datetime as _dt

    from .export import write_cheatsheet
    from .scoring import PRESETS
    from .valuation import LeagueConfig

    config = _league_config(args)
    manual = _read_manual_tiers(getattr(args, "tiers_file", None))
    prices = _read_fixed_prices(getattr(args, "tiers_file", None))
    notes = _read_tier_notes(getattr(args, "tiers_file", None))
    overrides = _read_depth_overrides(getattr(args, "depth_overrides", None))

    # Real depth charts (nflverse/ESPN) drive the "most likely backup" column;
    # on any fetch failure the packet falls back to the same-team heuristic.
    backups: dict = {}
    starters: dict = {}
    if not getattr(args, "no_depth", False):
        from .ingest import depth_backups, depth_starters

        depth_year = args.year or _dt.date.today().year
        try:
            backups = depth_backups(depth_year)
            starters = depth_starters(depth_year)
        except Exception as exc:  # noqa: BLE001 - packet still builds without depth
            print(f"warning: depth charts unavailable ({exc}); using heuristic backups")

    with _open_session(args) as session:
        path = write_cheatsheet(
            session, args.out, year=args.year, config=config,
            rules=PRESETS[args.scoring], basis=args.basis,
            manual_tiers=manual, fixed_prices=prices, tier_notes=notes,
            backups=backups, starters=starters, backup_overrides=overrides,
            ratings=_read_ratings(getattr(args, "tiers_file", None)),
        )
    print(f"Wrote draft packet to {path}")
    return 0


def _cmd_build_webapp(args: argparse.Namespace) -> int:
    import datetime as _dt

    from .export import write_webapp_data
    from .scoring import PRESETS
    from .valuation import LeagueConfig

    config = _league_config(args)
    manual = _read_manual_tiers(getattr(args, "tiers_file", None))
    seeds = _read_ratings(getattr(args, "tiers_file", None))  # seed app from master rating
    prices = _read_fixed_prices(getattr(args, "tiers_file", None))
    overrides = _read_depth_overrides(getattr(args, "depth_overrides", None))

    # Depth charts feed the in-browser personal packet's backup column; on any
    # fetch failure the app falls back to the same-team heuristic.
    backups: dict = {}
    starters: dict = {}
    if not getattr(args, "no_depth", False):
        from .ingest import depth_backups, depth_starters

        depth_year = args.year or _dt.date.today().year
        try:
            backups = depth_backups(depth_year)
            starters = depth_starters(depth_year)
        except Exception as exc:  # noqa: BLE001 - app still builds without depth
            print(f"warning: depth charts unavailable ({exc}); using heuristic backups")

    with _open_session(args) as session:
        path = write_webapp_data(
            session, args.out, year=args.year, config=config,
            rules=PRESETS[args.scoring], basis=args.basis, depth=args.depth,
            manual_tiers=manual, seed_overrides=seeds, prices=prices,
            backups=backups, starters=starters, backup_overrides=overrides,
        )
    print(f"Wrote pick-game data to {path}")
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
    p_values.add_argument("--price-cap", action="append", dest="price_caps",
                        metavar="POS=N", help="Market price ceiling, e.g. QB=30 (repeatable; POS=0 removes the default QB cap)")
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

    p_import = sub.add_parser("import-tiers", help="Rebuild master tiers by merging pick-game exports")
    p_import.add_argument("--file", required=True, nargs="+",
                         help="One or more app exports (key,rating,...); ratings are averaged")
    p_import.add_argument("--out", default="manual_tiers.csv", help="Output CSV path")
    p_import.add_argument("--prices-from", default=None, dest="prices_from",
                         help="Previous master: carry its ratings (un-picked players) and prices forward")
    p_import.add_argument("--confidence", type=int, default=6,
                         help="Comparisons for a 50%% user-rating weight vs the anchor (default 6)")
    p_import.set_defaults(func=_cmd_import_tiers)

    p_draft = sub.add_parser("load-draft", help="Add incoming rookies (top rounds) to the pool")
    p_draft.add_argument("--year", type=int, default=None, help="Draft year (default: current year)")
    p_draft.add_argument("--max-round", type=int, default=3, dest="max_round",
                         help="Include rookies drafted in rounds 1..N (default 3)")
    p_draft.set_defaults(func=_cmd_load_draft)

    p_coachtpl = sub.add_parser("coaching-template", help="Write a coaching CSV to fill in")
    p_coachtpl.add_argument("--out", default="coaching.csv", help="Output CSV path")
    p_coachtpl.set_defaults(func=_cmd_coaching_template)

    p_rz = sub.add_parser("load-redzone", help="Set red-zone targets from play-by-play")
    p_rz.add_argument("--year", type=int, default=None, help="Season (default: latest loaded)")
    p_rz.set_defaults(func=_cmd_load_redzone)

    p_byes = sub.add_parser("load-byes", help="Set team bye weeks from the schedule")
    p_byes.add_argument("--year", type=int, default=None, help="Season year (default: current year)")
    p_byes.set_defaults(func=_cmd_load_byes)

    p_coach = sub.add_parser("load-coaching", help="Load team coaching staff from a CSV")
    p_coach.add_argument("--file", required=True, help="CSV: team,head_coach,offensive_coordinator")
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
    p_sheet.add_argument("--price-cap", action="append", dest="price_caps",
                        metavar="POS=N", help="Market price ceiling, e.g. QB=30 (repeatable; POS=0 removes the default QB cap)")
    p_sheet.add_argument("--tiers-file", default=None, dest="tiers_file",
                        help="CSV of hard-set tiers (key,manual_tier) to override computed tiers")
    p_sheet.add_argument("--depth-overrides", default="depth_overrides.csv", dest="depth_overrides",
                        help="CSV of manual backup fixes (player,backup); wins over depth charts")
    p_sheet.add_argument("--no-depth", action="store_true", dest="no_depth",
                        help="Skip fetching depth charts (backups fall back to the heuristic)")
    p_sheet.set_defaults(func=_cmd_cheatsheet)

    p_webapp = sub.add_parser("build-webapp", help="Generate docs/data.js for the static pick game")
    p_webapp.add_argument("--out", default="docs/data.js", help="Output JS data path")
    p_webapp.add_argument("--year", type=int, default=None)
    p_webapp.add_argument("--basis", choices=["total", "ppg", "w3yr"], default="w3yr")
    p_webapp.add_argument("--scoring", choices=["standard", "half_ppr", "ppr"], default="half_ppr")
    p_webapp.add_argument("--teams", type=int, default=12)
    p_webapp.add_argument("--budget", type=int, default=200)
    p_webapp.add_argument("--price-cap", action="append", dest="price_caps",
                        metavar="POS=N", help="Market price ceiling, e.g. QB=30 (repeatable; POS=0 removes the default QB cap)")
    p_webapp.add_argument(
        "--depth", type=int, default=None,
        help="Max players per position in the pick game (default: draftable depth per position)",
    )
    p_webapp.add_argument("--tiers-file", default=None, dest="tiers_file",
                         help="CSV of hard-set tiers (key,manual_tier) to pin in the game")
    p_webapp.add_argument("--depth-overrides", default="depth_overrides.csv", dest="depth_overrides",
                         help="CSV of manual backup fixes (player,backup); wins over depth charts")
    p_webapp.add_argument("--no-depth", action="store_true", dest="no_depth",
                         help="Skip fetching depth charts (backups fall back to the heuristic)")
    p_webapp.set_defaults(func=_cmd_build_webapp)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
