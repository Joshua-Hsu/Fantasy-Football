"""Ingestion from nflverse.

nflverse publishes cleaned NFL data as files on GitHub. This package pulls a
season's teams, games, players and per-player weekly stat lines from those
files and loads them into our schema.
"""

from .nflverse import (
    load_active_roster,
    load_coaching,
    load_draft_rookies,
    load_games,
    load_players,
    load_season,
    load_seasons,
    load_team_stats,
    load_teams,
    load_weekly_stats,
    latest_head_coaches,
)

__all__ = [
    "load_teams",
    "load_players",
    "load_games",
    "load_weekly_stats",
    "load_team_stats",
    "load_active_roster",
    "load_coaching",
    "load_draft_rookies",
    "latest_head_coaches",
    "load_season",
    "load_seasons",
]
