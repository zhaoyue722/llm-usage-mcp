"""Engine factory, session factory, and SQLite tuning.

The DB URL is resolved from `LLM_USAGE_DB_URL`, defaulting to the spec's
`~/.llm-usage/usage.db`. File-backed SQLite engines get WAL mode and
`synchronous=NORMAL` applied on every connect — the standard pairing that
allows concurrent reads during writes without an fsync on every commit.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Final

from sqlalchemy import Engine, event
from sqlalchemy import create_engine as _sa_create_engine
from sqlalchemy.engine.url import make_url
from sqlalchemy.orm import Session, sessionmaker

DEFAULT_DB_PATH: Final[Path] = Path.home() / ".llm-usage" / "usage.db"

_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def resolve_db_url() -> str:
    """Return the DB URL, reading `LLM_USAGE_DB_URL` first.

    Used by both the application engine factory and `alembic/env.py` so app
    code and migrations always talk to the same database.
    """
    env_url = os.environ.get("LLM_USAGE_DB_URL")
    if env_url:
        return env_url
    return f"sqlite:///{DEFAULT_DB_PATH}"


def _is_file_sqlite(url: str) -> bool:
    parsed = make_url(url)
    if not parsed.drivername.startswith("sqlite"):
        return False
    db = parsed.database
    return bool(db) and db != ":memory:"


def _apply_sqlite_pragmas(dbapi_connection: Any, _record: Any) -> None:
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
    finally:
        cursor.close()


def create_engine(url: str | None = None) -> Engine:
    """Build a SQLAlchemy Engine.

    For file-backed SQLite URLs, ensures the parent directory exists and
    registers a `connect` listener that sets `journal_mode=WAL` and
    `synchronous=NORMAL` on every checkout. In-memory and non-SQLite URLs
    skip both behaviors.
    """
    resolved = url or resolve_db_url()

    if _is_file_sqlite(resolved):
        db_path = make_url(resolved).database
        assert db_path is not None  # _is_file_sqlite guarantees this
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    engine = _sa_create_engine(resolved, future=True)

    if _is_file_sqlite(resolved):
        event.listen(engine, "connect", _apply_sqlite_pragmas)

    return engine


def get_engine() -> Engine:
    """Return the lazily-built process-wide engine."""
    global _engine
    if _engine is None:
        _engine = create_engine()
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    """Return the lazily-built process-wide session factory."""
    global _session_factory
    if _session_factory is None:
        _session_factory = sessionmaker(
            get_engine(),
            autoflush=False,
            expire_on_commit=False,
        )
    return _session_factory


def get_session() -> Session:
    """Open a new ORM session bound to the process-wide engine."""
    return get_session_factory()()
