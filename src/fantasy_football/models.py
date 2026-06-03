"""SQLAlchemy ORM models for the football stats database.

The schema is normalized around the core entities you find on a football
stats site:

- ``Team``      - a franchise (e.g. Green Bay Packers).
- ``Season``    - a single year of competition.
- ``Player``    - a person, with biographical info.
- ``Game``      - a single matchup between two teams in a season.
- ``PlayerGameStats`` - one player's box-score line for one game. This is the
  grain that most fantasy scoring is computed from.
- ``TeamGameStats``   - one team's box-score line for one game.

Counting stats default to ``0`` rather than ``NULL`` so that aggregation
(``SUM``, fantasy scoring) never has to special-case missing values. Rate
stats (passer rating, etc.) are nullable because "no attempts" is genuinely
not zero.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    relationship,
)


class Base(DeclarativeBase):
    """Declarative base for all models."""


# Number of seconds-less stat columns share a default of 0; defined here so
# the intent (counting stats are never NULL) is stated once.
def _count() -> Mapped[int]:
    return mapped_column(Integer, nullable=False, default=0, server_default="0")


class Team(Base):
    """An NFL franchise.

    ``abbreviation`` is the stable short code used to join external data
    sources (e.g. ``GNB``, ``NWE``). A franchise that relocates keeps one row;
    historical names can be tracked later if needed.
    """

    __tablename__ = "teams"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    abbreviation: Mapped[str] = mapped_column(String(8), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    location: Mapped[str | None] = mapped_column(String(64))
    conference: Mapped[str | None] = mapped_column(String(8))  # AFC / NFC
    division: Mapped[str | None] = mapped_column(String(16))  # North / South / ...
    active: Mapped[bool] = mapped_column(Integer, nullable=False, default=1, server_default="1")

    # Coaching staff (loaded from a user-supplied CSV; shown on player cards).
    head_coach: Mapped[str | None] = mapped_column(String(64))
    offensive_coordinator: Mapped[str | None] = mapped_column(String(64))
    play_caller: Mapped[str | None] = mapped_column(String(64))
    bye_week: Mapped[int | None] = mapped_column(Integer)

    home_games: Mapped[list[Game]] = relationship(
        back_populates="home_team", foreign_keys="Game.home_team_id"
    )
    away_games: Mapped[list[Game]] = relationship(
        back_populates="away_team", foreign_keys="Game.away_team_id"
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<Team {self.abbreviation} {self.name!r}>"


class Season(Base):
    """A single season, keyed by its starting year (e.g. 2024)."""

    __tablename__ = "seasons"

    year: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    regular_season_weeks: Mapped[int | None] = mapped_column(Integer)

    games: Mapped[list[Game]] = relationship(back_populates="season")

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<Season {self.year}>"


class Player(Base):
    """A football player and their biographical details.

    ``slug`` mirrors the stable identifier external sources use for a player
    (e.g. Pro Football Reference's ``RodgAa00``), giving us a clean key to
    de-duplicate against when ingesting data.
    """

    __tablename__ = "players"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str | None] = mapped_column(String(32), unique=True)
    full_name: Mapped[str] = mapped_column(String(128), nullable=False)
    first_name: Mapped[str | None] = mapped_column(String(64))
    last_name: Mapped[str | None] = mapped_column(String(64))
    position: Mapped[str | None] = mapped_column(String(8))  # QB, RB, WR, ...
    birth_date: Mapped[dt.date | None] = mapped_column(Date)
    height_inches: Mapped[int | None] = mapped_column(Integer)
    weight_lbs: Mapped[int | None] = mapped_column(Integer)
    college: Mapped[str | None] = mapped_column(String(128))
    debut_year: Mapped[int | None] = mapped_column(Integer)

    # Current-season roster status (set by load_active_roster). ``active`` marks
    # players on an NFL roster for the target season, so the draft pool can be
    # limited to selectable players (and rookies, who have no stats yet).
    current_team: Mapped[str | None] = mapped_column(String(8))
    status: Mapped[str | None] = mapped_column(String(8))
    active: Mapped[bool] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    rookie_year: Mapped[int | None] = mapped_column(Integer)
    draft_round: Mapped[int | None] = mapped_column(Integer)
    draft_pick: Mapped[int | None] = mapped_column(Integer)

    game_stats: Mapped[list[PlayerGameStats]] = relationship(back_populates="player")

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<Player {self.full_name!r} ({self.position})>"


class Game(Base):
    """A single game between two teams within a season.

    ``season_type`` distinguishes regular season from postseason/preseason so
    that stat aggregation can scope to the relevant games.
    """

    __tablename__ = "games"
    __table_args__ = (
        UniqueConstraint("season_year", "week", "home_team_id", "away_team_id", name="uq_game"),
        CheckConstraint("home_team_id != away_team_id", name="ck_game_distinct_teams"),
        CheckConstraint(
            "season_type IN ('regular', 'postseason', 'preseason')",
            name="ck_game_season_type",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    season_year: Mapped[int] = mapped_column(ForeignKey("seasons.year"), nullable=False)
    week: Mapped[int] = mapped_column(Integer, nullable=False)
    season_type: Mapped[str] = mapped_column(
        String(16), nullable=False, default="regular", server_default="regular"
    )
    game_date: Mapped[dt.date | None] = mapped_column(Date)

    home_team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"), nullable=False)
    away_team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"), nullable=False)
    home_score: Mapped[int | None] = mapped_column(Integer)
    away_score: Mapped[int | None] = mapped_column(Integer)

    stadium: Mapped[str | None] = mapped_column(String(128))
    attendance: Mapped[int | None] = mapped_column(Integer)

    season: Mapped[Season] = relationship(back_populates="games")
    home_team: Mapped[Team] = relationship(back_populates="home_games", foreign_keys=[home_team_id])
    away_team: Mapped[Team] = relationship(back_populates="away_games", foreign_keys=[away_team_id])
    player_stats: Mapped[list[PlayerGameStats]] = relationship(back_populates="game")

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<Game {self.season_year} wk{self.week} {self.away_team_id}@{self.home_team_id}>"


class PlayerGameStats(Base):
    """One player's box-score statistics for a single game.

    This is the workhorse table: a wide, denormalized stat line covering
    passing, rushing, receiving, kicking and returns. ``team_id`` records who
    the player suited up for in this game (it can differ across a season after
    a trade). One row per (player, game).
    """

    __tablename__ = "player_game_stats"
    __table_args__ = (
        UniqueConstraint("player_id", "game_id", name="uq_player_game"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id"), nullable=False)
    game_id: Mapped[int] = mapped_column(ForeignKey("games.id"), nullable=False)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"), nullable=False)

    # Snapshot fields useful for box scores.
    position: Mapped[str | None] = mapped_column(String(8))
    started: Mapped[bool] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    # --- Passing ---
    pass_completions: Mapped[int] = _count()
    pass_attempts: Mapped[int] = _count()
    pass_yards: Mapped[int] = _count()
    pass_touchdowns: Mapped[int] = _count()
    interceptions_thrown: Mapped[int] = _count()
    times_sacked: Mapped[int] = _count()
    sacked_yards: Mapped[int] = _count()
    passer_rating: Mapped[float | None] = mapped_column(Float)

    # --- Rushing ---
    rush_attempts: Mapped[int] = _count()
    rush_yards: Mapped[int] = _count()
    rush_touchdowns: Mapped[int] = _count()

    # --- Receiving ---
    targets: Mapped[int] = _count()
    receptions: Mapped[int] = _count()
    receiving_yards: Mapped[int] = _count()
    receiving_touchdowns: Mapped[int] = _count()

    # --- Ball security ---
    fumbles: Mapped[int] = _count()
    fumbles_lost: Mapped[int] = _count()

    # --- Kicking ---
    field_goals_made: Mapped[int] = _count()
    field_goals_attempted: Mapped[int] = _count()
    extra_points_made: Mapped[int] = _count()
    extra_points_attempted: Mapped[int] = _count()
    # Field goals made by distance bucket, for distance-based scoring. These sum
    # to ``field_goals_made``; the total is kept for convenience/validation.
    fg_made_0_19: Mapped[int] = _count()
    fg_made_20_29: Mapped[int] = _count()
    fg_made_30_39: Mapped[int] = _count()
    fg_made_40_49: Mapped[int] = _count()
    fg_made_50_59: Mapped[int] = _count()
    fg_made_60_plus: Mapped[int] = _count()

    # --- Returns ---
    kick_return_yards: Mapped[int] = _count()
    kick_return_touchdowns: Mapped[int] = _count()
    punt_return_yards: Mapped[int] = _count()
    punt_return_touchdowns: Mapped[int] = _count()

    player: Mapped[Player] = relationship(back_populates="game_stats")
    game: Mapped[Game] = relationship(back_populates="player_stats")
    team: Mapped[Team] = relationship()

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<PlayerGameStats player={self.player_id} game={self.game_id}>"


class TeamGameStats(Base):
    """One team's box-score totals for a single game.

    Team-level totals are stored separately from the sum of player lines so
    that team stats remain available even when player-level detail is missing,
    and so team rates (total yards, turnovers, time of possession) have a home.
    """

    __tablename__ = "team_game_stats"
    __table_args__ = (
        UniqueConstraint("team_id", "game_id", name="uq_team_game"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"), nullable=False)
    game_id: Mapped[int] = mapped_column(ForeignKey("games.id"), nullable=False)
    is_home: Mapped[bool] = mapped_column(Integer, nullable=False)

    # Offense
    points: Mapped[int] = _count()
    total_yards: Mapped[int] = _count()
    plays: Mapped[int] = _count()
    passing_yards: Mapped[int] = _count()
    rushing_yards: Mapped[int] = _count()
    turnovers: Mapped[int] = _count()
    first_downs: Mapped[int] = _count()
    time_of_possession_seconds: Mapped[int | None] = mapped_column(Integer)

    # Defense / special teams (this team's defensive production in the game).
    # ``points_allowed`` / ``yards_allowed`` are what the opponent's offense did,
    # and drive the tiered DST scoring.
    points_allowed: Mapped[int] = _count()
    yards_allowed: Mapped[int] = _count()
    sacks: Mapped[int] = _count()
    interceptions: Mapped[int] = _count()
    fumble_recoveries: Mapped[int] = _count()
    defensive_tds: Mapped[int] = _count()
    special_teams_tds: Mapped[int] = _count()
    safeties: Mapped[int] = _count()

    team: Mapped[Team] = relationship()
    game: Mapped[Game] = relationship()

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<TeamGameStats team={self.team_id} game={self.game_id}>"


class UserRating(Base):
    """A human-derived "user rating" for one draftable entity.

    The rating is an Elo-style score seeded from the computed auction value and
    nudged by head-to-head picks in the comparison game. ``key`` is the stable
    entity id used across the toolkit (``p<player_id>`` / ``d<team_abbr>``) so a
    player and a team defense can both have a rating.
    """

    __tablename__ = "user_ratings"

    key: Mapped[str] = mapped_column(String(16), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    position: Mapped[str] = mapped_column(String(8), nullable=False)
    rating: Mapped[float] = mapped_column(Float, nullable=False)
    comparisons: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime, nullable=False, default=dt.datetime.utcnow
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<UserRating {self.key} {self.rating:.0f}>"


class Comparison(Base):
    """An append-only log of head-to-head picks (winner chosen over loser)."""

    __tablename__ = "comparisons"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    position: Mapped[str] = mapped_column(String(8), nullable=False)
    winner_key: Mapped[str] = mapped_column(String(16), nullable=False)
    loser_key: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime, nullable=False, default=dt.datetime.utcnow
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<Comparison {self.winner_key} > {self.loser_key}>"


# Timestamp mixin columns could be added per-table later; kept minimal for the
# initial base schema.
__all__ = [
    "Base",
    "Team",
    "Season",
    "Player",
    "Game",
    "PlayerGameStats",
    "TeamGameStats",
    "UserRating",
    "Comparison",
]
