"""Round-trip tests for the Alembic migration scripts.

Asserts that `alembic upgrade head` produces the same set of tables as the
declarative models, that the partial unique index keeps its `WHERE` clause,
that the `schema_version` table is seeded, and that `downgrade base` is clean.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine

from llm_usage.core import Base

REPO_ROOT = Path(__file__).resolve().parent.parent
ALEMBIC_INI = REPO_ROOT / "alembic.ini"


@pytest.fixture
def db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "usage.db"
    monkeypatch.setenv("LLM_USAGE_DB_URL", f"sqlite:///{path}")
    return path


@pytest.fixture
def alembic_config() -> Config:
    return Config(str(ALEMBIC_INI))


@pytest.fixture
def upgraded_engine(db_path: Path, alembic_config: Config) -> Iterator[Engine]:
    command.upgrade(alembic_config, "head")
    eng = create_engine(f"sqlite:///{db_path}", future=True)
    try:
        yield eng
    finally:
        eng.dispose()


def test_upgrade_creates_spec_tables(upgraded_engine: Engine) -> None:
    inspector = inspect(upgraded_engine)
    tables = set(inspector.get_table_names())
    # alembic_version is Alembic's own; the rest must match the spec.
    assert {
        "usage_events",
        "pricing_snapshot",
        "quality_snapshot",
        "schema_version",
    } <= tables
    assert "alembic_version" in tables


def test_upgrade_matches_model_metadata(upgraded_engine: Engine) -> None:
    inspector = inspect(upgraded_engine)
    for table in Base.metadata.sorted_tables:
        cols_db = {c["name"] for c in inspector.get_columns(table.name)}
        cols_model = {c.name for c in table.columns}
        assert cols_db == cols_model, table.name


def test_partial_unique_index_preserved(upgraded_engine: Engine) -> None:
    """The WHERE clause on idx_events_request_id is what makes recording idempotent."""
    with upgraded_engine.connect() as conn:
        ddl = conn.execute(
            text("SELECT sql FROM sqlite_master WHERE name = 'idx_events_request_id'")
        ).scalar_one()
    assert "WHERE request_id IS NOT NULL" in ddl


def test_schema_version_seeded(upgraded_engine: Engine) -> None:
    with upgraded_engine.connect() as conn:
        version = conn.execute(text("SELECT version FROM schema_version")).scalar_one()
    assert version == 1


def test_downgrade_removes_spec_tables(db_path: Path, alembic_config: Config) -> None:
    command.upgrade(alembic_config, "head")
    command.downgrade(alembic_config, "base")

    eng = create_engine(f"sqlite:///{db_path}", future=True)
    try:
        inspector = inspect(eng)
        tables = set(inspector.get_table_names())
    finally:
        eng.dispose()

    assert "usage_events" not in tables
    assert "pricing_snapshot" not in tables
    assert "quality_snapshot" not in tables
    assert "schema_version" not in tables
