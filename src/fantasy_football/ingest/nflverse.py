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

from ..models import Game, Player, PlayerGameStats, Season, Team

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

    # Returns (return TDs aren't split by type in the source -> left at 0)
    line.kick_return_yards = _count(g("kickoff_return_yards"))
    line.punt_return_yards = _count(g("punt_return_yards"))


def load_season(session: Session, year: int) -> dict[str, int]:
    """Load everything for one season: teams, season, games, players, stats.

    Returns a summary dict of how many rows were written per entity.
    """
    teams_written = len(_ensure_teams(session))
    games = load_games(session, year)
    players = load_players(session, year)
    stats = load_weekly_stats(session, year)
    return {"teams": teams_written, "games": games, "players": players, "stat_lines": stats}


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
