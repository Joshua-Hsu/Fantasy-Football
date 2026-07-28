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


def _read_tier_pins(
    path: str | None, column: str = "tier_pin"
) -> tuple[dict[str, int], set[str]]:
    """Read commissioner tier pins from a CSV column.

    Returns ``(pins, released)``: ``pins`` maps key -> pinned integer tier;
    ``released`` is the set of keys whose row is an explicit un-pin (empty
    rating AND empty tier — the app's "release to crowd" rows). The master
    carries pins in ``tier_pin``; an admin overwrite file carries them in
    ``tier``. Same hardening as the other readers.
    """
    import csv
    import os

    if not path or not os.path.exists(path):
        return {}, set()
    pins: dict[str, int] = {}
    released: set[str] = set()
    with open(path, newline="") as fh:
        reader = csv.DictReader(fh)
        if column not in (reader.fieldnames or []):
            return {}, set()
        for i, row in enumerate(reader):
            if i >= _MAX_TIERS_ROWS:
                break
            key = (row.get("key") or "").strip()
            if not _KEY_RE.match(key):
                continue
            value = (row.get(column) or "").strip()
            if not value:
                if not (row.get("rating") or "").strip():
                    released.add(key)
                continue
            try:
                tier = int(float(value))
            except ValueError:
                continue
            if 1 <= tier <= 40:
                pins[key] = tier
    return pins, released


def _read_yahoo_values(path: str | None) -> dict:
    """{normalized name: (proj$, avg$, fp$)} from a yahoo/values.<date>.csv.

    Reads both the API and manual-snapshot formats (name, proj_value,
    avg_cost, optional fp_value columns); blank/non-numeric dollars stay
    blank. fp_value is the FantasyPros auction value ridealong.
    """
    import csv
    import os

    from .export import _norm_name

    if not path or not os.path.exists(path):
        return {}
    out: dict = {}

    def num(v):
        try:
            return round(float(v))
        except (TypeError, ValueError):
            return ""

    with open(path, newline="") as fh:
        reader = csv.DictReader(fh)
        if "name" not in (reader.fieldnames or []):
            return {}
        for i, row in enumerate(reader):
            if i >= _MAX_TIERS_ROWS:
                break
            name = (row.get("name") or "").strip()
            if name:
                out[_norm_name(name)] = (num(row.get("proj_value")),
                                         num(row.get("avg_cost")),
                                         num(row.get("fp_value")))
    return out


def _latest_yahoo_file(dirpath: str = "yahoo") -> str | None:
    """Newest yahoo/values.<date>.csv that actually has data rows."""
    import glob
    import os

    for path in sorted(glob.glob(os.path.join(dirpath, "values.*.csv")),
                       reverse=True):
        try:
            with open(path) as fh:
                if len(fh.readlines()) > 1:
                    return path
        except OSError:
            continue
    return None


def _read_pool_overrides(path: str | None) -> set[str]:
    """Read manual pool includes: CSV with ``player[,treat]`` columns.

    Names (lowercased) of players forced into the app pool and seeded like
    rookies by draft capital — for injury returnees with no recent production
    (the reason the pool's stat-based selection misses them).
    """
    import csv
    import os

    if not path or not os.path.exists(path):
        return set()
    names: set[str] = set()
    with open(path, newline="") as fh:
        reader = csv.DictReader(fh)
        if "player" not in (reader.fieldnames or []):
            return set()
        for i, row in enumerate(reader):
            if i >= _MAX_TIERS_ROWS:
                break
            player = (row.get("player") or "").strip()
            if player:
                names.add(player.lower()[:64])
    return names


def _master_base(path: str | None) -> str:
    """Short content hash identifying a master tiers file.

    Embedded in data.js (``FF_DATA.base``) and echoed by the app in every
    commit, so a rebuild can tell which master a pick file was played
    against. ANY change to the master (crowd blend, admin pin, price edit)
    yields a new base — picks made on a previous master never shift the
    next rebuild (their signal already lives in the master's ratings).
    """
    import hashlib
    import os

    if not path or not os.path.exists(path):
        return ""
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()[:10]


def _read_base(path: str | None) -> str:
    """The ``base`` stamp a pick file was made against ('' if unstamped)."""
    import csv
    import os

    if not path or not os.path.exists(path):
        return ""
    with open(path, newline="") as fh:
        reader = csv.DictReader(fh)
        if "base" not in (reader.fieldnames or []):
            return ""
        for i, row in enumerate(reader):
            if i >= _MAX_TIERS_ROWS:
                break
            value = (row.get("base") or "").strip().lower()
            if re.fullmatch(r"[0-9a-f]{4,16}", value):
                return value
    return ""


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


def _pick_leaderboard(dirs: list[str]) -> dict[str, int]:
    """{user id: best lifetime pick count} from every u-<id>.csv on disk.

    The app's comps counters are lifetime totals per device, so a user's most
    recent submission already contains their whole history: across weekly
    archives we take the MAX per id, never the sum. Pick count ~= total
    comps / 2 (each matchup touches two players). Per-player comps are
    clamped by _read_comps, which also caps how far a gamed file can inflate
    its owner's level.
    """
    import glob
    import os
    import re as _re

    best: dict[str, int] = {}
    for d in dirs:
        if not os.path.isdir(d):
            continue
        for path in glob.glob(os.path.join(d, "**", "u-*.csv"), recursive=True):
            m = _re.match(r"u-([a-z0-9]{4,40})\.csv$", os.path.basename(path), _re.I)
            if not m:
                continue
            uid = m.group(1)
            total = sum(_read_comps(path).values()) // 2
            if total > best.get(uid, -1):
                best[uid] = total
    return best


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
    pinned = _read_ratings(getattr(args, "admin_file", None))  # commissioner pins
    # Commissioner TIER pins: a fresh admin overwrite (tier column) is
    # literal; pins carried from the previous master's tier_pin column act
    # as bands the fresh crowd can move players across; released keys drop.
    prev_pins, _ = _read_tier_pins(getattr(args, "prices_from", None))
    admin_pins, admin_released = _read_tier_pins(
        getattr(args, "admin_file", None), column="tier")
    carried_pins = {k: t for k, t in prev_pins.items()
                    if k not in admin_released and k not in admin_pins}
    # Freshness gate: only pick files stamped with THIS master's base shift
    # the blend. Picks made on a previous master are already baked into the
    # carried ratings — replaying them would double-count stale opinions
    # (and could drag players across admin tiers the picker never saw).
    cur_base = _master_base(getattr(args, "prices_from", None))
    if cur_base:
        blend_files = [f for f in files if _read_base(f) == cur_base]
        stale = len(files) - len(blend_files)
    else:
        blend_files, stale = files, 0
    user_ratings = _merge_ratings(blend_files, base={})   # picked players only
    comps = _merge_comps(blend_files)
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
            pinned_ratings=pinned or None,
            pinned_tiers=admin_pins or None,
            carried_tiers=carried_pins or None,
            confidence=getattr(args, "confidence", None) or 6,
            tiers=legacy_tiers or None,
            prices=existing_prices, notes=notes or None,
        )
    blended = sum(1 for k in user_ratings if comps.get(k))
    if stale:
        print(f"Skipped {stale} pick file(s) made on a previous master "
              f"(base != {cur_base}); their signal is already carried.")
    if pinned:
        print(f"Applied {len(pinned)} admin rating pin(s).")
    if admin_pins or carried_pins:
        print(f"Tier pins: {len(admin_pins)} literal (fresh overwrite), "
              f"{len(carried_pins)} carried as crowd-movable bands"
              + (f", {len(admin_released)} released." if admin_released else "."))
    elif admin_released:
        print(f"Released {len(admin_released)} player(s) back to derived tiers.")
    print(f"Merged {len(files)} pick file(s) -> {args.out}: "
          f"{len(user_ratings)} picked ({blended} confidence-blended; base carried: "
          f"{len(base_ratings)}), prices preserved: {len(existing_prices)}. "
          f"Rebuild the board/app to apply.")
    return 0


def _cmd_audit_picks(args: argparse.Namespace) -> int:
    """Flag junk pick files; optionally quarantine them out of the blend.

    Prints one machine-readable line per finding (``FLAG|path|reason``) plus a
    GitHub Actions ``::warning::`` for run logs. With ``--quarantine DIR``,
    flagged files are moved there so the rebuild's ``picks/*.csv`` glob no
    longer sees them. Exit code is 0 either way - workflows react to output,
    not failures.
    """
    import os
    import shutil

    from .audit import audit_pick_files

    files = args.file if isinstance(args.file, list) else [args.file]
    files = [f for f in files if os.path.exists(f)]
    master = _read_ratings(getattr(args, "master", None))
    findings = audit_pick_files(files, master, max_files=args.max_files)

    if not findings:
        print(f"audit clean: {len(files)} pick file(s) look legitimate")
        return 0
    for f in findings:
        print(f"FLAG|{f.path}|{f.reason}")
        print(f"::warning file={f.path}::{f.reason}")
        if args.quarantine and os.path.isfile(f.path):
            os.makedirs(args.quarantine, exist_ok=True)
            shutil.move(f.path, os.path.join(args.quarantine, os.path.basename(f.path)))
            print(f"quarantined {f.path} -> {args.quarantine}/")
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
    pins, _ = _read_tier_pins(getattr(args, "tiers_file", None))
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
            pinned_tiers=pins or None,
            yahoo_values=_read_yahoo_values(_latest_yahoo_file()) or None,
        )
    print(f"Wrote draft packet to {path}")
    return 0


def _cmd_pull_yahoo(args: argparse.Namespace) -> int:
    """Snapshot Yahoo's salary-cap values into a dated CSV (+ raw HTML).

    Designed for the weekly GitHub Action: raw pages are ALWAYS saved (so a
    markup change never loses a week of history — it can be re-parsed), and
    the run only fails when nothing at all came back.
    """
    import csv
    import datetime as _dt
    import os

    from .yahoo import (fetch_salcap_api, fetch_salcap_pages,
                        fetch_salcap_pages_browser, parse_players_api_json,
                        parse_salcap_html)

    today = _dt.date.today().isoformat()
    raw_dir = os.path.join(args.out_dir, "raw", today)
    os.makedirs(raw_dir, exist_ok=True)

    # Preferred source: the Fantasy Sports API (OAuth secrets in the env) —
    # the web page locks the salcap value columns behind Fantasy Plus, but
    # the API serves them to any authenticated account. Fallbacks: rendered
    # browser (--browser), then static fetch.
    creds = tuple(os.environ.get(k, "") for k in
                  ("YAHOO_CLIENT_ID", "YAHOO_CLIENT_SECRET", "YAHOO_REFRESH_TOKEN"))
    rows: list[dict] = []
    seen: set[str] = set()
    fetched_any = False
    if all(creds):
        pages = fetch_salcap_api(*creds)
        fetched_any = fetched_any or bool(pages)
        for start, text in pages:
            with open(os.path.join(raw_dir, f"api-{start}.json"), "w") as fh:
                fh.write(text)
            for row in parse_players_api_json(text):
                if row["name"] not in seen:
                    seen.add(row["name"])
                    rows.append(row)
        if not rows:
            print("note: API yielded no rows (Fantasy Sports authorization "
                  "pending?) - falling back to the public page")
    if not rows:
        if getattr(args, "browser", False):
            pages = fetch_salcap_pages_browser()
        else:
            pages = fetch_salcap_pages()
        fetched_any = fetched_any or bool(pages)
        for offset, html in pages:
            with open(os.path.join(raw_dir, f"salcap-{offset}.html"), "w") as fh:
                fh.write(html)
            for row in parse_salcap_html(html):
                if row["name"] not in seen:
                    seen.add(row["name"])
                    rows.append(row)

    if not fetched_any:
        print("error: no salcap pages fetched (Yahoo unreachable or blocking)")
        return 1

    out = os.path.join(args.out_dir, f"values.{today}.csv")
    with open(out, "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=[
            "date", "name", "team_pos", "proj_value", "avg_cost",
            "pct_drafted", "all_dollars"])
        writer.writeheader()
        for row in rows:
            writer.writerow({"date": today, **row})
    print(f"Saved {len(rows)} players -> {out} "
          f"({len(pages)} raw pages under {raw_dir})")
    if not rows:
        print("warning: 0 rows parsed - raw pages saved for a parser fix/re-parse")
    return 0


def _cmd_build_webapp(args: argparse.Namespace) -> int:
    import datetime as _dt

    from .export import write_webapp_data
    from .scoring import PRESETS
    from .valuation import LeagueConfig

    config = _league_config(args)
    manual = _read_manual_tiers(getattr(args, "tiers_file", None))
    seeds = _read_ratings(getattr(args, "tiers_file", None))  # seed app from master rating
    pins, _ = _read_tier_pins(getattr(args, "tiers_file", None))  # commissioner law
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

    # All-time pick leaderboard for the app's levels page: current inbox plus
    # every weekly archive (max per user - comps are lifetime counters).
    leaders = _pick_leaderboard(["picks", "archive"])

    with _open_session(args) as session:
        path = write_webapp_data(
            session, args.out, year=args.year, config=config,
            rules=PRESETS[args.scoring], basis=args.basis, depth=args.depth,
            manual_tiers=manual, seed_overrides=seeds, pinned_tiers=pins or None,
            prices=prices,
            backups=backups, starters=starters, backup_overrides=overrides,
            leaders=leaders,
            base=_master_base(getattr(args, "tiers_file", None)),
            extra_rookies=_read_pool_overrides(getattr(args, "pool_overrides", None)),
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
    p_import.add_argument("--admin-file", default=None, dest="admin_file",
                         help="Commissioner overwrite CSV (key,rating); pins applied after the blend")
    p_import.add_argument("--confidence", type=int, default=6,
                         help="Comparisons for a 50%% user-rating weight vs the anchor (default 6)")
    p_import.set_defaults(func=_cmd_import_tiers)

    p_audit = sub.add_parser("audit-picks", help="Flag junk pick files (unknown keys, off-scale, clones)")
    p_audit.add_argument("--file", required=True, nargs="+", help="Pick files to audit")
    p_audit.add_argument("--master", default=None, help="Newest master CSV (defines pool + rating scale)")
    p_audit.add_argument("--quarantine", default=None, help="Move flagged files into this directory")
    p_audit.add_argument("--max-files", type=int, default=300, dest="max_files",
                        help="Inbox volume tripwire (default 300)")
    p_audit.set_defaults(func=_cmd_audit_picks)

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
    p_webapp.add_argument("--pool-overrides", default="pool_overrides.csv",
                         dest="pool_overrides",
                         help="CSV of players (player[,treat]) forced into the pool "
                              "and seeded like rookies (injury returnees)")
    p_webapp.set_defaults(func=_cmd_build_webapp)

    p_yahoo = sub.add_parser(
        "pull-yahoo",
        help="Snapshot Yahoo's salary-cap draft values (projected + current avg)")
    p_yahoo.add_argument("--out-dir", default="yahoo", dest="out_dir",
                         help="Directory for values.<date>.csv + raw/ snapshots")
    p_yahoo.add_argument("--browser", action="store_true",
                         help="Render with headless Chromium (needs playwright); "
                              "required for real pulls - Yahoo's table is JS-built")
    p_yahoo.set_defaults(func=_cmd_pull_yahoo)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
