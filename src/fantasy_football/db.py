"""Database engine and session helpers.

Centralizes how the application connects to SQLite so that the rest of the
codebase never has to build a connection string by hand. The default location
is ``data/fantasy_football.db`` relative to the project root, overridable via
the ``FANTASY_FOOTBALL_DB`` environment variable.
"""

from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from .models import Base

# Project root is three levels up from this file: src/fantasy_football/db.py
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "fantasy_football.db"

ENV_VAR = "FANTASY_FOOTBALL_DB"


def database_url(db_path: str | os.PathLike[str] | None = None) -> str:
    """Build a SQLite SQLAlchemy URL.

    Resolution order: explicit ``db_path`` arg, then ``$FANTASY_FOOTBALL_DB``,
    then the default ``data/`` location. The special value ``:memory:`` is
    passed through for ephemeral in-memory databases (handy in tests).
    """

    if db_path is None:
        db_path = os.environ.get(ENV_VAR, str(DEFAULT_DB_PATH))

    if str(db_path) == ":memory:":
        return "sqlite+pysqlite:///:memory:"

    return f"sqlite+pysqlite:///{Path(db_path)}"


def create_db_engine(
    db_path: str | os.PathLike[str] | None = None, *, echo: bool = False
) -> Engine:
    """Create an :class:`~sqlalchemy.Engine`, enabling SQLite foreign keys.

    SQLite does not enforce foreign keys unless ``PRAGMA foreign_keys = ON`` is
    issued per-connection, so we register a hook to do that for every new
    connection.
    """

    url = database_url(db_path)

    # On-disk databases: make sure the parent directory exists.
    if ":memory:" not in url:
        path = Path(url.replace("sqlite+pysqlite:///", ""))
        path.parent.mkdir(parents=True, exist_ok=True)

    engine = create_engine(url, echo=echo, future=True)

    @event.listens_for(engine, "connect")
    def _enable_sqlite_fk(dbapi_connection, _connection_record):  # noqa: ANN001
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys = ON")
        cursor.close()

    return engine


def init_db(engine: Engine) -> None:
    """Create all tables defined on :data:`Base.metadata` if they don't exist."""

    Base.metadata.create_all(engine)


def get_sessionmaker(engine: Engine) -> sessionmaker[Session]:
    """Return a configured :class:`~sqlalchemy.orm.sessionmaker`."""

    return sessionmaker(bind=engine, future=True, expire_on_commit=False)
