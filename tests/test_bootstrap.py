"""Tests for `llm_usage.bootstrap`.

Each test gets a fresh DB file via `LLM_USAGE_DB_URL`. The conftest
autouse fixture already disposes and nulls the session module
singletons, so `get_session()` rebuilds against the new URL.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import func, inspect, select

from llm_usage.bootstrap import (
    _find_alembic_root,
    bootstrap,
    materialize_pricing,
    migrate_to_head,
)
from llm_usage.core.db.models import PricingSnapshot, QualitySnapshot
from llm_usage.core.db.session import get_engine, get_session


@pytest.fixture
def db_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """Point `Settings.db_url` at a fresh, empty SQLite file for one test."""
    url = f"sqlite:///{tmp_path / 'usage.db'}"
    monkeypatch.setenv("LLM_USAGE_DB_URL", url)
    return url


def _table_names() -> set[str]:
    return set(inspect(get_engine()).get_table_names())


def test_find_alembic_root_returns_directory_with_ini_and_alembic_dir() -> None:
    root = _find_alembic_root()
    assert (root / "alembic.ini").is_file()
    assert (root / "alembic").is_dir()


def test_migrate_to_head_creates_all_tables(db_url: str) -> None:
    migrate_to_head()
    names = _table_names()
    # The three application tables plus alembic's bookkeeping.
    assert {"usage_events", "pricing_snapshot", "schema_version"} <= names
    assert "alembic_version" in names


def test_migrate_to_head_is_idempotent(db_url: str) -> None:
    migrate_to_head()
    migrate_to_head()  # second run is a no-op; must not raise
    assert "usage_events" in _table_names()


def test_materialize_pricing_populates_empty_table(db_url: str) -> None:
    migrate_to_head()
    written = materialize_pricing()
    assert written > 0
    with get_session() as session:
        row_count = session.scalar(select(func.count()).select_from(PricingSnapshot))
    assert row_count == written


def test_materialize_pricing_refreshes_on_every_call(db_url: str) -> None:
    """Second `materialize_pricing` call upserts the full set again.

    Regression for the old "first-run only" guard: after the first
    materialize, the function used to short-circuit on any non-empty
    table, which meant edits to `pricing_overrides.json` never reached
    `pricing_snapshot` on restart. Here we delete a row to stand in
    for "an override just added a model that wasn't there before" and
    assert the next materialize restores it.
    """
    migrate_to_head()
    first = materialize_pricing()
    assert first > 0

    with get_session() as session:
        baseline = {
            (row.provider, row.model) for row in session.scalars(select(PricingSnapshot)).all()
        }
        # Drop one row to simulate a row that "isn't there yet but should be".
        target_provider, target_model = next(iter(baseline))
        session.query(PricingSnapshot).filter_by(
            provider=target_provider, model=target_model
        ).delete()
        session.commit()

    second = materialize_pricing()
    assert second == first  # full set re-asserted, not zero

    with get_session() as session:
        after = {
            (row.provider, row.model) for row in session.scalars(select(PricingSnapshot)).all()
        }
    assert after == baseline  # deleted row was restored


def test_quality_snapshot_table_exists_but_stays_empty(db_url: str) -> None:
    """v1 reserves the table for the post-v1 quality importer; it's created
    by the migration but never populated by `bootstrap()`."""
    bootstrap()
    assert "quality_snapshot" in _table_names()
    with get_session() as session:
        quality_count = session.scalar(select(func.count()).select_from(QualitySnapshot))
    assert quality_count == 0


def test_bootstrap_runs_migrations_and_seeds_pricing(db_url: str) -> None:
    bootstrap()

    assert "usage_events" in _table_names()
    with get_session() as session:
        pricing_count = session.scalar(select(func.count()).select_from(PricingSnapshot))
    assert pricing_count is not None and pricing_count > 0


def test_bootstrap_is_safe_to_call_twice(db_url: str) -> None:
    bootstrap()
    with get_session() as session:
        pricing_before = session.scalar(select(func.count()).select_from(PricingSnapshot))

    bootstrap()
    with get_session() as session:
        pricing_after = session.scalar(select(func.count()).select_from(PricingSnapshot))

    assert pricing_after == pricing_before


def test_bootstrap_creates_missing_parent_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: fresh install where the DB's parent dir doesn't exist.

    Mirrors the production failure mode where `~/.llm-usage/` is not
    yet created on first server boot. Alembic's own engine factory
    doesn't auto-create directories, so without our explicit
    `_ensure_sqlite_parent_dir` step, SQLite raises `OperationalError:
    unable to open database file` before any migration script runs.
    """
    nested = tmp_path / "deeper" / "subdir"
    db = nested / "usage.db"
    monkeypatch.setenv("LLM_USAGE_DB_URL", f"sqlite:///{db}")

    assert not nested.exists()
    bootstrap()
    assert nested.is_dir()
    assert db.is_file()
