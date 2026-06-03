"""Load NFL data from nflverse into the stats schema.

Data comes from files nflverse publishes on GitHub (no scraping, no
rate-limited hosts):

- **Teams**   ``nflfastR-data/teams_colors_logos.csv``
- **Games**   ``nfldata/data/games.csv`` (schedules + results, 1999-present)
- **Weekly**  ``nflverse-data`` ``stats_player`` release - one row per player
              per game, covering passing/rushing/receiving/kicking/returns
- **Rosters** ``nflverse-data`` ``rosters`` release - player biographical data

Loaders are idempotent: each row is matched to an existing record by its
natural key and updated in place, so re-running a season refreshes scores and
stats rather than duplicating them. This is what makes in-season re-loads safe.

Note: the official ``nfl_data_py`` package hard-codes some source URLs that are
either behind a blocked host (schedules via ``habitatring.com``) or point at an
older release that lacks recent seasons. We therefore read the underlying
nflverse files directly via pandas, which keeps this module self-contained.
"""

from __future__ import annotations

import datetime as dt
import time
from typing import Any, Callable

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Game, Player, PlayerGameStats, Season, Team, TeamGameStats

# --- Source URLs -----------------------------------------------------------

TEAMS_URL = "https://github.com/nflverse/nflfastR-data/raw/master/teams_colors_logos.csv"
GAMES_URL = "https://raw.githubusercontent.com/nflverse/nfldata/master/data/games.csv"
WEEKLY_URL = (
    "https://github.com/nflverse/nflverse-data/releases/download/"
    "stats_player/stats_player_week_{year}.parquet"
)
ROSTER_URL = (
    "https://github.com/nflverse/nflverse-data/releases/download/"
    "rosters/roster_{year}.parquet"
)
TEAM_WEEK_URL = (
    "https://github.com/nflverse/nflverse-data/releases/download/"
    "stats_team/stats_team_week_{year}.parquet"
)
DRAFT_URL = (
    "https://github.com/nflverse/nflverse-data/releases/download/"
    "draft_picks/draft_picks.parquet"
)

# Team-code aliases: roster/draft sources use a few codes that differ from the
# canonical nflverse stats codes. Normalize so current_team/team join correctly.
_TEAM_ALIASES = {
    "AZ": "ARI", "ARZ": "ARI", "LAR": "LA", "STL": "LA", "RAM": "LA",
    "SD": "LAC", "SDG": "LAC", "OAK": "LV", "LVR": "LV", "BLT": "BAL",
    "CLV": "CLE", "HST": "HOU", "GNB": "GB", "KAN": "KC", "NWE": "NE",
    "NOR": "NO", "SFO": "SF", "TAM": "TB",
}


def _norm_team(code: str | None) -> str | None:
    """Map a source team code to the canonical nflverse abbreviation."""
    if not code:
        return code
    return _TEAM_ALIASES.get(code.upper(), code)

#: nflverse weekly/roster data begins in 1999; weekly *player* stats are
#: reliable from the early 2000s. Used as a floor for season ranges.
EARLIEST_SEASON = 1999


# --- Value coercion --------------------------------------------------------
# pandas represents missing values as NaN/NaT. These helpers turn those into
# the right Python value for our columns: 0 for counting stats, None for
# nullable fields.


def _count(value: Any) -> int:
    """Coerce a possibly-missing numeric value to a non-negative int (NaN -> 0)."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return 0
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return 0


def _opt_int(value: Any) -> int | None:
    if value is None or pd.isna(value):
        return None
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return None


def _opt_float(value: Any) -> float | None:
    if value is None or pd.isna(value):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _opt_str(value: Any) -> str | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    text = str(value).strip()
    return text or None


def _opt_date(value: Any) -> dt.date | None:
    if value is None or value == "" or pd.isna(value):
        return None
    ts = pd.to_datetime(value, errors="coerce")
    return None if pd.isna(ts) else ts.date()


# --- Fetching --------------------------------------------------------------


def _read(reader: Callable[[], pd.DataFrame], *, retries: int = 4) -> pd.DataFrame:
    """Run a pandas read with simple exponential-backoff retries.

    Guards against transient network hiccups when pulling from GitHub.
    """
    delay = 2.0
    last: Exception | None = None
    for attempt in range(retries):
        try:
            return reader()
        except Exception as exc:  # noqa: BLE001 - re-raised below after retries
            last = exc
            if attempt == retries - 1:
                break
            time.sleep(delay)
            delay *= 2
    assert last is not None
    raise last


def _fetch_teams() -> pd.DataFrame:
    return _read(lambda: pd.read_csv(TEAMS_URL))


def _fetch_games() -> pd.DataFrame:
    return _read(lambda: pd.read_csv(GAMES_URL, low_memory=False))


def _fetch_weekly(year: int) -> pd.DataFrame:
    return _read(lambda: pd.read_parquet(WEEKLY_URL.format(year=year)))


def _fetch_roster(year: int) -> pd.DataFrame:
    return _read(lambda: pd.read_parquet(ROSTER_URL.format(year=year)))


def _fetch_team_week(year: int) -> pd.DataFrame:
    return _read(lambda: pd.read_parquet(TEAM_WEEK_URL.format(year=year)))


def _fetch_draft() -> pd.DataFrame:
    return _read(lambda: pd.read_parquet(DRAFT_URL))


FANTASY_DRAFT_POSITIONS = {"QB", "RB", "WR", "TE"}


def load_draft_rookies(session: Session, year: int, *, max_round: int = 3) -> int:
    """Add incoming rookies (drafted in rounds 1..``max_round``) to the pool.

    Drafted rookies aren't in the stats/roster files yet, so they're created here
    from ``draft_picks`` as active placeholders (no stats) with their draft round
    and pick. Only fantasy-relevant positions are added. Returns rows written.
    """
    df = _fetch_draft()
    df = df[(df["season"] == year) & (df["round"] <= max_round)]
    existing = {p.slug: p for p in session.scalars(select(Player)) if p.slug}
    written = 0
    for row in df.itertuples():
        pos = _opt_str(getattr(row, "position", None))
        if pos not in FANTASY_DRAFT_POSITIONS:
            continue
        slug = _opt_str(getattr(row, "gsis_id", None)) or _opt_str(
            getattr(row, "pfr_player_id", None)
        )
        if not slug:
            continue
        team = _norm_team(_opt_str(getattr(row, "team", None)))
        player = existing.get(slug)
        if player is None:
            player = Player(slug=slug, full_name=_opt_str(row.pfr_player_name) or slug)
            session.add(player)
            existing[slug] = player
        if _opt_str(getattr(row, "pfr_player_name", None)):
            player.full_name = _opt_str(row.pfr_player_name)
        player.position = pos
        player.current_team = team
        player.status = player.status or "ROOKIE"
        player.active = True
        player.rookie_year = year
        player.draft_round = _opt_int(getattr(row, "round", None))
        player.draft_pick = _opt_int(getattr(row, "pick", None))
        if player.college is None:
            player.college = _opt_str(getattr(row, "college", None))
        written += 1
    session.commit()
    return written


def season_has_data(year: int) -> bool:
    """Return True if nflverse has weekly player stats for ``year``.

    Used to resolve "most recent season": future/unplayed seasons exist in the
    schedule but have no stat file yet (HTTP 404).
    """
    try:
        _fetch_weekly(year)
        return True
    except Exception:  # noqa: BLE001 - any fetch failure means "not available"
        return False


# --- Mapping helpers -------------------------------------------------------


def _season_type_from_game_type(game_type: Any) -> str:
    """Map an nflverse ``game_type`` to our ``games.season_type`` value."""
    gt = (str(game_type) or "").upper()
    if gt == "REG":
        return "regular"
    if gt == "PRE":
        return "preseason"
    # WC / DIV / CON / SB and anything else postseason.
    return "postseason"


def _division_parts(team_conf: Any, team_division: Any) -> tuple[str | None, str | None]:
    """Split nflverse conf/division (e.g. 'NFC', 'NFC West') into (conf, 'West')."""
    conf = _opt_str(team_conf)
    div = _opt_str(team_division)
    if div and conf and div.upper().startswith(conf.upper()):
        div = div[len(conf):].strip()
    return conf, div or None


# --- Loaders ---------------------------------------------------------------


def load_teams(session: Session) -> int:
    """Upsert all franchises from nflverse. Returns number of rows written."""
    df = _fetch_teams().drop_duplicates(subset=["team_abbr"])
    existing = {t.abbreviation: t for t in session.scalars(select(Team))}
    written = 0
    for row in df.itertuples():
        abbr = _opt_str(row.team_abbr)
        if not abbr:
            continue
        conf, div = _division_parts(
            getattr(row, "team_conf", None), getattr(row, "team_division", None)
        )
        name = _opt_str(getattr(row, "team_nick", None)) or _opt_str(row.team_name) or abbr
        team = existing.get(abbr)
        if team is None:
            team = Team(abbreviation=abbr)
            session.add(team)
            existing[abbr] = team
        team.name = name
        team.location = _opt_str(row.team_name)
        team.conference = conf
        team.division = div
        written += 1
    session.commit()
    return written


def _ensure_teams(session: Session) -> dict[str, int]:
    """Make sure teams exist and return an {abbreviation: id} cache."""
    if session.scalar(select(Team).limit(1)) is None:
        load_teams(session)
    else:
        session.commit()
    return {t.abbreviation: t.id for t in session.scalars(select(Team))}


def load_players(session: Session, year: int) -> int:
    """Upsert players (keyed on nflverse player id -> ``players.slug``).

    Biographical fields come from the season roster; the weekly stats file is
    used as a fallback so that anyone who recorded a stat line still gets a
    player row even if they are absent from the roster snapshot.
    """
    existing = {p.slug: p for p in session.scalars(select(Player)) if p.slug}
    written = 0

    def upsert(slug: str | None, *, name: str | None, position: str | None, **bio: Any) -> None:
        nonlocal written
        if not slug:
            return
        player = existing.get(slug)
        if player is None:
            player = Player(slug=slug, full_name=name or slug)
            session.add(player)
            existing[slug] = player
        if name:
            player.full_name = name
        if position:
            player.position = position
        for attr, value in bio.items():
            if value is not None:
                setattr(player, attr, value)
        written += 1

    roster = _fetch_roster(year)
    for row in roster.itertuples():
        upsert(
            _opt_str(getattr(row, "gsis_id", None)),
            name=_opt_str(getattr(row, "full_name", None)),
            position=_opt_str(getattr(row, "position", None)),
            first_name=_opt_str(getattr(row, "first_name", None)),
            last_name=_opt_str(getattr(row, "last_name", None)),
            birth_date=_opt_date(getattr(row, "birth_date", None)),
            height_inches=_opt_int(getattr(row, "height", None)),
            weight_lbs=_opt_int(getattr(row, "weight", None)),
            college=_opt_str(getattr(row, "college", None)),
        )

    # Fallback: anyone with a stat line but no roster row.
    weekly = _fetch_weekly(year)
    for row in weekly[["player_id", "player_display_name", "position"]].drop_duplicates().itertuples():
        slug = _opt_str(row.player_id)
        if slug and slug not in existing:
            upsert(
                slug,
                name=_opt_str(row.player_display_name),
                position=_opt_str(row.position),
            )

    session.commit()
    return written


def load_games(session: Session, year: int) -> int:
    """Upsert the season row and all of its games. Returns games written."""
    teams = _ensure_teams(session)

    if session.get(Season, year) is None:
        session.add(Season(year=year))

    df = _fetch_games()
    df = df[df["season"] == year]

    existing = {
        (g.season_year, g.week, g.home_team_id, g.away_team_id): g
        for g in session.scalars(select(Game).where(Game.season_year == year))
    }
    written = 0
    for row in df.itertuples():
        home_id = teams.get(_opt_str(row.home_team))
        away_id = teams.get(_opt_str(row.away_team))
        if home_id is None or away_id is None:
            continue  # unknown team code; skip rather than violate FK
        key = (year, int(row.week), home_id, away_id)
        game = existing.get(key)
        if game is None:
            game = Game(
                season_year=year,
                week=int(row.week),
                home_team_id=home_id,
                away_team_id=away_id,
            )
            session.add(game)
            existing[key] = game
        game.season_type = _season_type_from_game_type(row.game_type)
        game.game_date = _opt_date(getattr(row, "gameday", None))
        game.home_score = _opt_int(getattr(row, "home_score", None))
        game.away_score = _opt_int(getattr(row, "away_score", None))
        game.stadium = _opt_str(getattr(row, "stadium", None))
        written += 1
    session.commit()
    return written


def load_weekly_stats(session: Session, year: int) -> int:
    """Upsert per-player box-score lines for a season. Returns lines written.

    Each weekly row is matched to a game by ``(week, {team, opponent})`` since
    the stats file carries no game id. Counting stats absent from the source
    (return TDs, passer rating) keep their schema defaults.
    """
    teams = _ensure_teams(session)
    players = {p.slug: p.id for p in session.scalars(select(Player)) if p.slug}

    # Index this season's games by (week, frozenset of team ids).
    games = list(session.scalars(select(Game).where(Game.season_year == year)))
    game_by_matchup: dict[tuple[int, frozenset[int]], int] = {
        (g.week, frozenset({g.home_team_id, g.away_team_id})): g.id for g in games
    }
    game_ids = {g.id for g in games}

    # Preload existing stat lines for these games for idempotent upsert.
    existing: dict[tuple[int, int], PlayerGameStats] = {}
    if game_ids:
        for line in session.scalars(
            select(PlayerGameStats).where(PlayerGameStats.game_id.in_(game_ids))
        ):
            existing[(line.player_id, line.game_id)] = line

    df = _fetch_weekly(year)
    written = 0
    for row in df.itertuples():
        slug = _opt_str(row.player_id)
        team_id = teams.get(_opt_str(row.team))
        opp_id = teams.get(_opt_str(row.opponent_team))
        player_id = players.get(slug) if slug else None
        if player_id is None or team_id is None or opp_id is None:
            continue
        game_id = game_by_matchup.get((int(row.week), frozenset({team_id, opp_id})))
        if game_id is None:
            continue  # no matching game (e.g. data gap); skip rather than guess

        line = existing.get((player_id, game_id))
        if line is None:
            line = PlayerGameStats(player_id=player_id, game_id=game_id, team_id=team_id)
            session.add(line)
            existing[(player_id, game_id)] = line
        _apply_stats(line, row, team_id)
        written += 1
    session.commit()
    return written


def _apply_stats(line: PlayerGameStats, row: Any, team_id: int) -> None:
    """Copy a weekly DataFrame row onto a PlayerGameStats record."""
    g = lambda name: getattr(row, name, None)  # noqa: E731 - terse local accessor

    line.team_id = team_id
    line.position = _opt_str(g("position"))

    # Passing
    line.pass_completions = _count(g("completions"))
    line.pass_attempts = _count(g("attempts"))
    line.pass_yards = _count(g("passing_yards"))
    line.pass_touchdowns = _count(g("passing_tds"))
    line.interceptions_thrown = _count(g("passing_interceptions"))
    line.times_sacked = _count(g("sacks_suffered"))
    line.sacked_yards = _count(g("sack_yards_lost"))

    # Rushing
    line.rush_attempts = _count(g("carries"))
    line.rush_yards = _count(g("rushing_yards"))
    line.rush_touchdowns = _count(g("rushing_tds"))

    # Receiving
    line.targets = _count(g("targets"))
    line.receptions = _count(g("receptions"))
    line.receiving_yards = _count(g("receiving_yards"))
    line.receiving_touchdowns = _count(g("receiving_tds"))

    # Ball security (summed across the source's split fumble columns)
    line.fumbles = (
        _count(g("sack_fumbles")) + _count(g("rushing_fumbles")) + _count(g("receiving_fumbles"))
    )
    line.fumbles_lost = (
        _count(g("sack_fumbles_lost"))
        + _count(g("rushing_fumbles_lost"))
        + _count(g("receiving_fumbles_lost"))
    )

    # Kicking
    line.field_goals_made = _count(g("fg_made"))
    line.field_goals_attempted = _count(g("fg_att"))
    line.extra_points_made = _count(g("pat_made"))
    line.extra_points_attempted = _count(g("pat_att"))
    # FG made by distance bucket (source's 60+ column is `fg_made_60_`).
    line.fg_made_0_19 = _count(g("fg_made_0_19"))
    line.fg_made_20_29 = _count(g("fg_made_20_29"))
    line.fg_made_30_39 = _count(g("fg_made_30_39"))
    line.fg_made_40_49 = _count(g("fg_made_40_49"))
    line.fg_made_50_59 = _count(g("fg_made_50_59"))
    line.fg_made_60_plus = _count(g("fg_made_60_"))

    # Returns (return TDs aren't split by type in the source -> left at 0)
    line.kick_return_yards = _count(g("kickoff_return_yards"))
    line.punt_return_yards = _count(g("punt_return_yards"))


def load_team_stats(session: Session, year: int) -> int:
    """Upsert one ``TeamGameStats`` row per (team, game) for a season.

    Offensive totals come straight from the team-week file. Defensive output
    (sacks, takeaways, def/ST TDs, safeties) comes from the same row's ``def_*``
    fields. ``points``/``points_allowed`` come from the game's score, and
    ``yards_allowed`` is the opponent's offensive yardage in that game (looked
    up from the opponent's own row). Returns rows written.
    """
    teams = _ensure_teams(session)

    games = list(session.scalars(select(Game).where(Game.season_year == year)))
    game_by_matchup: dict[tuple[int, frozenset[int]], Game] = {
        (g.week, frozenset({g.home_team_id, g.away_team_id})): g for g in games
    }
    game_ids = {g.id for g in games}

    existing: dict[tuple[int, int], TeamGameStats] = {}
    if game_ids:
        for tgs in session.scalars(
            select(TeamGameStats).where(TeamGameStats.game_id.in_(game_ids))
        ):
            existing[(tgs.team_id, tgs.game_id)] = tgs

    df = _fetch_team_week(year)

    # First pass: index each team's offensive yards by (week, team_id) so we can
    # resolve an opponent's yards_allowed.
    off_yards: dict[tuple[int, int], int] = {}
    rows: list[tuple[Any, int, int, Game]] = []
    for row in df.itertuples():
        team_id = teams.get(_opt_str(row.team))
        opp_id = teams.get(_opt_str(row.opponent_team))
        if team_id is None or opp_id is None:
            continue
        game = game_by_matchup.get((int(row.week), frozenset({team_id, opp_id})))
        if game is None:
            continue
        yards = _count(getattr(row, "passing_yards", 0)) + _count(getattr(row, "rushing_yards", 0))
        off_yards[(int(row.week), team_id)] = yards
        rows.append((row, team_id, opp_id, game))

    written = 0
    for row, team_id, opp_id, game in rows:
        is_home = team_id == game.home_team_id
        team_score = game.home_score if is_home else game.away_score
        opp_score = game.away_score if is_home else game.home_score

        tgs = existing.get((team_id, game.id))
        if tgs is None:
            tgs = TeamGameStats(team_id=team_id, game_id=game.id, is_home=is_home)
            session.add(tgs)
            existing[(team_id, game.id)] = tgs
        tgs.is_home = is_home

        g = lambda name: getattr(row, name, None)  # noqa: E731 - terse accessor
        passing = _count(g("passing_yards"))
        rushing = _count(g("rushing_yards"))
        tgs.passing_yards = passing
        tgs.rushing_yards = rushing
        tgs.total_yards = passing + rushing
        # Offensive plays = pass attempts + rush attempts + sacks taken.
        tgs.plays = _count(g("attempts")) + _count(g("carries")) + _count(g("sacks_suffered"))
        tgs.points = _opt_int(team_score) or 0
        tgs.points_allowed = _opt_int(opp_score) or 0
        tgs.yards_allowed = off_yards.get((int(row.week), opp_id), 0)
        tgs.turnovers = (
            _count(g("passing_interceptions"))
            + _count(g("sack_fumbles_lost"))
            + _count(g("rushing_fumbles_lost"))
            + _count(g("receiving_fumbles_lost"))
        )
        # Defense / special teams
        tgs.sacks = _count(g("def_sacks"))
        tgs.interceptions = _count(g("def_interceptions"))
        tgs.fumble_recoveries = _count(g("fumble_recovery_opp"))
        tgs.defensive_tds = _count(g("def_tds"))
        tgs.special_teams_tds = _count(g("special_teams_tds"))
        tgs.safeties = _count(g("def_safeties"))
        written += 1

    session.commit()
    return written


#: Roster statuses that mean a player is on an NFL roster (draftable). Unsigned
#: free agents (UFA/RFA/UDF) and cut/retired players are not "active".
ACTIVE_STATUSES = {"ACT", "RES", "PUP", "DEV"}


def load_active_roster(session: Session, year: int) -> int:
    """Mark the selectable player pool from a season's roster.

    Sets ``current_team``/``status``/``active``/``rookie_year`` on each player
    and creates rows for players who have no stats yet (rookies), so the draft
    pool can be limited to players actually on an NFL roster for ``year``.
    Returns the number of active players. Re-running refreshes status; players
    not on this roster are marked inactive.
    """
    df = _fetch_roster(year)
    existing = {p.slug: p for p in session.scalars(select(Player)) if p.slug}

    # Anyone previously active but absent from this roster becomes inactive.
    seen: set[str] = set()
    active = 0
    for row in df.itertuples():
        slug = _opt_str(getattr(row, "gsis_id", None))
        if not slug:
            continue
        seen.add(slug)
        status = _opt_str(getattr(row, "status", None))
        is_active = (status or "").upper() in ACTIVE_STATUSES

        player = existing.get(slug)
        if player is None:
            player = Player(slug=slug, full_name=_opt_str(getattr(row, "full_name", None)) or slug)
            session.add(player)
            existing[slug] = player
        # Roster is the source of truth for current team/position.
        player.current_team = _norm_team(_opt_str(getattr(row, "team", None)))
        player.status = status
        player.active = is_active
        player.position = _opt_str(getattr(row, "position", None)) or player.position
        player.rookie_year = _opt_int(getattr(row, "rookie_year", None)) or _opt_int(
            getattr(row, "entry_year", None)
        )
        # Fill bio for brand-new (rookie) rows.
        if not player.birth_date:
            player.birth_date = _opt_date(getattr(row, "birth_date", None))
            player.height_inches = player.height_inches or _opt_int(getattr(row, "height", None))
            player.weight_lbs = player.weight_lbs or _opt_int(getattr(row, "weight", None))
            player.college = player.college or _opt_str(getattr(row, "college", None))
        if is_active:
            active += 1

    for slug, player in existing.items():
        if slug not in seen and player.active:
            player.active = False

    session.commit()
    return active


def latest_head_coaches() -> dict[str, str]:
    """{team_abbr: head coach} from the most recent season in the schedule."""
    df = _fetch_games()
    df = df[df["season"] == int(df["season"].max())]
    coaches: dict[str, str] = {}
    for row in df.sort_values("week").itertuples():
        for team_attr, coach_attr in (("home_team", "home_coach"), ("away_team", "away_coach")):
            team = _opt_str(getattr(row, team_attr, None))
            coach = _opt_str(getattr(row, coach_attr, None))
            if team and coach:
                coaches[team] = coach  # later weeks overwrite -> end-of-season staff
    return coaches


def team_byes(year: int) -> dict[str, int]:
    """{team_abbr: bye_week} for a season, derived from the schedule.

    A team's bye is the regular-season week (1-18) in which it has no game.
    """
    df = _fetch_games()
    df = df[(df["season"] == year) & (df["game_type"] == "REG")]
    teams = set(df["home_team"]) | set(df["away_team"])
    byes: dict[str, int] = {}
    for team in teams:
        played = set(int(w) for w in df[(df["home_team"] == team) | (df["away_team"] == team)]["week"])
        missing = [w for w in range(1, 19) if w not in played]
        abbr = _norm_team(_opt_str(team))
        if abbr and missing:
            byes[abbr] = missing[0]
    return byes


def load_byes(session: Session, year: int) -> int:
    """Set each team's bye week for ``year`` from the schedule. Returns count."""
    byes = team_byes(year)
    teams = {t.abbreviation: t for t in session.scalars(select(Team))}
    updated = 0
    for abbr, week in byes.items():
        team = teams.get(abbr)
        if team is not None:
            team.bye_week = week
            updated += 1
    session.commit()
    return updated


def load_coaching(session: Session, path: str) -> int:
    """Load team coaching staff from a CSV: team,head_coach,offensive_coordinator,play_caller."""
    import csv

    teams = {t.abbreviation: t for t in session.scalars(select(Team))}
    updated = 0
    with open(path, newline="") as fh:
        for row in csv.DictReader(fh):
            team = teams.get((row.get("team") or "").strip().upper())
            if team is None:
                continue
            team.head_coach = _opt_str(row.get("head_coach")) or team.head_coach
            team.offensive_coordinator = (
                _opt_str(row.get("offensive_coordinator")) or team.offensive_coordinator
            )
            team.play_caller = _opt_str(row.get("play_caller")) or team.play_caller
            updated += 1
    session.commit()
    return updated


def load_season(session: Session, year: int) -> dict[str, int]:
    """Load everything for one season: teams, season, games, players, stats.

    Returns a summary dict of how many rows were written per entity.
    """
    teams_written = len(_ensure_teams(session))
    games = load_games(session, year)
    players = load_players(session, year)
    stats = load_weekly_stats(session, year)
    team_stats = load_team_stats(session, year)
    return {
        "teams": teams_written,
        "games": games,
        "players": players,
        "stat_lines": stats,
        "team_lines": team_stats,
    }


def load_seasons(
    session: Session, start: int, end: int | None = None
) -> dict[int, dict[str, int]]:
    """Load a range of seasons, skipping any that have no data yet.

    ``end`` defaults to the current calendar year; seasons in the range that
    nflverse has not published stats for (future/unplayed) are skipped, so the
    range naturally resolves to "... through the most recent played season".
    """
    if end is None:
        end = dt.date.today().year
    start = max(start, EARLIEST_SEASON)

    load_teams(session)  # once up front
    results: dict[int, dict[str, int]] = {}
    for year in range(start, end + 1):
        if not season_has_data(year):
            continue
        results[year] = load_season(session, year)
    return results
