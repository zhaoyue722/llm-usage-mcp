"""Smoke tests for the engine factory and session module."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import select

import llm_usage.core.db.session as session_mod
from llm_usage.core import (
    Base,
    UsageEvent,
    create_engine,
    resolve_db_url,
)


@pytest.fixture
def reset_session_singletons() -> Iterator[None]:
    """Reset the module-level engine + session factory around each test.

    Without this, a test that calls `get_engine()` leaves a SQLite connection
    pool live across tests, which trips pytest's `filterwarnings = ["error"]`
    when the connection is later GC'd.
    """
    original_engine = session_mod._engine
    original_factory = session_mod._session_factory
    session_mod._engine = None
    session_mod._session_factory = None
    try:
        yield
    finally:
        if session_mod._engine is not None:
            session_mod._engine.dispose()
        session_mod._engine = original_engine
        session_mod._session_factory = original_factory


def test_resolve_db_url_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LLM_USAGE_DB_URL", raising=False)
    url = resolve_db_url()
    assert url.startswith("sqlite:///")
    assert url.endswith(".llm-usage/usage.db")


def test_resolve_db_url_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_USAGE_DB_URL", "sqlite:///custom.db")
    assert resolve_db_url() == "sqlite:///custom.db"


def test_create_engine_creates_parent_dir(tmp_path: Path) -> None:
    """A nested DB path should have its parent created on engine build."""
    db_path = tmp_path / "nested" / "subdir" / "usage.db"
    assert not db_path.parent.exists()

    engine = create_engine(f"sqlite:///{db_path}")
    try:
        assert db_path.parent.is_dir()
    finally:
        engine.dispose()


def test_wal_mode_applied_on_connect(tmp_path: Path) -> None:
    """File-backed SQLite engines should boot with journal_mode=WAL and synchronous=NORMAL."""
    db_path = tmp_path / "usage.db"
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        Base.metadata.create_all(engine)
        with engine.connect() as conn:
            mode = conn.exec_driver_sql("PRAGMA journal_mode").scalar_one()
            sync = conn.exec_driver_sql("PRAGMA synchronous").scalar_one()
        # SQLite returns lowercase "wal"; synchronous=NORMAL is integer 1.
        assert str(mode).lower() == "wal"
        assert int(sync) == 1
    finally:
        engine.dispose()


def test_in_memory_engine_skips_pragmas(tmp_path: Path) -> None:
    """`:memory:` databases can't use WAL — engine builds but mode stays default."""
    engine = create_engine("sqlite:///:memory:")
    try:
        with engine.connect() as conn:
            mode = conn.exec_driver_sql("PRAGMA journal_mode").scalar_one()
        # In-memory SQLite reports "memory", not "wal".
        assert str(mode).lower() != "wal"
    finally:
        engine.dispose()


def test_insert_and_query_round_trip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reset_session_singletons: None,
) -> None:
    """End-to-end: build engine via the lazy singleton, write a row, read it back."""
    db_path = tmp_path / "usage.db"
    monkeypatch.setenv("LLM_USAGE_DB_URL", f"sqlite:///{db_path}")

    engine = session_mod.get_engine()
    Base.metadata.create_all(engine)

    factory = session_mod.get_session_factory()
    with factory() as session:
        session.add(
            UsageEvent(
                id="evt-1",
                timestamp=1_700_000_000_000,
                provider="anthropic",
                model="claude-sonnet-4-6",
                input_tokens=100,
                output_tokens=200,
                cost_usd=0.0042,
            )
        )
        session.commit()

    with factory() as session:
        event = session.scalars(select(UsageEvent)).one()
        assert event.id == "evt-1"
        assert event.provider == "anthropic"
        assert event.input_tokens == 100
        assert event.output_tokens == 200
        assert event.cost_usd == pytest.approx(0.0042)
        # success defaults to True per the spec.
        assert event.success is True
