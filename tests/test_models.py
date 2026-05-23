"""Schema-level tests for the SQLAlchemy models.

These tests stand the schema up in an in-memory SQLite database and assert that
the columns, defaults, indexes, and constraints match `docs/spec.md`.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from llm_usage.core import (
    CURRENT_SCHEMA_VERSION,
    Base,
    PricingSnapshot,
    SchemaVersion,
    UsageEvent,
)


@pytest.fixture
def engine() -> Iterator[Engine]:
    eng = create_engine("sqlite://", future=True)
    Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()


def test_tables_match_spec(engine: Engine) -> None:
    inspector = inspect(engine)
    assert set(inspector.get_table_names()) == {
        "usage_events",
        "pricing_snapshot",
        "pricing_tier",
        "quality_snapshot",
        "schema_version",
    }


def test_usage_events_columns(engine: Engine) -> None:
    inspector = inspect(engine)
    cols = {c["name"]: c for c in inspector.get_columns("usage_events")}

    expected = {
        "id",
        "timestamp",
        "provider",
        "model",
        "input_tokens",
        "output_tokens",
        "cache_write_tokens",
        "cache_read_tokens",
        "cost_nano_usd",
        "duration_ms",
        "success",
        "error_type",
        "request_id",
        "project",
        "tags",
        "metadata",
    }
    assert set(cols) == expected

    # Spec: id is the primary key.
    pk = inspector.get_pk_constraint("usage_events")
    assert pk["constrained_columns"] == ["id"]

    # Spec: token columns and success have NOT NULL with defaults.
    for col_name in (
        "timestamp",
        "provider",
        "model",
        "input_tokens",
        "output_tokens",
        "cache_write_tokens",
        "cache_read_tokens",
        "cost_nano_usd",
        "success",
    ):
        assert cols[col_name]["nullable"] is False, col_name

    for col_name in ("duration_ms", "error_type", "request_id", "project", "tags", "metadata"):
        assert cols[col_name]["nullable"] is True, col_name


def test_usage_events_indexes(engine: Engine) -> None:
    inspector = inspect(engine)
    indexes = {ix["name"]: ix for ix in inspector.get_indexes("usage_events")}

    assert "idx_events_timestamp" in indexes
    assert indexes["idx_events_timestamp"]["column_names"] == ["timestamp"]

    assert "idx_events_provider_model" in indexes
    assert indexes["idx_events_provider_model"]["column_names"] == ["provider", "model"]

    assert "idx_events_project" in indexes
    assert indexes["idx_events_project"]["column_names"] == ["project"]

    assert "idx_events_request_id" in indexes
    assert bool(indexes["idx_events_request_id"]["unique"]) is True


def test_request_id_uniqueness_is_partial(engine: Engine) -> None:
    """Two NULL request_ids must coexist; two equal non-NULL must collide."""
    with Session(engine) as session:
        session.add(
            UsageEvent(
                id="evt-a",
                timestamp=1,
                provider="anthropic",
                model="claude-sonnet-4-6",
                cost_nano_usd=0,
            )
        )
        session.add(
            UsageEvent(
                id="evt-b",
                timestamp=2,
                provider="anthropic",
                model="claude-sonnet-4-6",
                cost_nano_usd=0,
            )
        )
        session.commit()

        session.add(
            UsageEvent(
                id="evt-c",
                timestamp=3,
                provider="openai",
                model="gpt-4o",
                cost_nano_usd=0,
                request_id="req-1",
            )
        )
        session.commit()

        session.add(
            UsageEvent(
                id="evt-d",
                timestamp=4,
                provider="openai",
                model="gpt-4o",
                cost_nano_usd=0,
                request_id="req-1",
            )
        )
        with pytest.raises(Exception, match="UNIQUE constraint failed"):
            session.commit()
        session.rollback()


def test_token_defaults_apply_on_raw_insert(engine: Engine) -> None:
    """Raw SQL insert that omits token columns must rely on the column server-defaults."""
    with engine.begin() as conn:
        raw = conn.connection.driver_connection
        assert isinstance(raw, sqlite3.Connection)
        raw.execute(
            "INSERT INTO usage_events (id, timestamp, provider, model, cost_nano_usd) "
            "VALUES (?, ?, ?, ?, ?)",
            ("evt-1", 1, "openai", "gpt-4o", 0),
        )
        row = raw.execute(
            "SELECT input_tokens, output_tokens, cache_write_tokens, cache_read_tokens, success "
            "FROM usage_events WHERE id = ?",
            ("evt-1",),
        ).fetchone()
        assert row == (0, 0, 0, 0, 1)


def test_pricing_snapshot_composite_pk(engine: Engine) -> None:
    inspector = inspect(engine)
    pk = inspector.get_pk_constraint("pricing_snapshot")
    assert pk["constrained_columns"] == ["provider", "model"]


def test_pricing_snapshot_roundtrip(engine: Engine) -> None:
    with Session(engine) as session:
        session.add(
            PricingSnapshot(
                provider="anthropic",
                model="claude-sonnet-4-6",
                input_per_million_usd=3.0,
                output_per_million_usd=15.0,
                cache_write_per_million_usd=3.75,
                cache_read_per_million_usd=0.30,
                fetched_at=1_700_000_000_000,
            )
        )
        session.commit()

        row = session.scalars(select(PricingSnapshot)).one()
        assert row.provider == "anthropic"
        assert row.cache_read_per_million_usd == 0.30


def test_schema_version_seed(engine: Engine) -> None:
    with Session(engine) as session:
        session.add(SchemaVersion(version=CURRENT_SCHEMA_VERSION))
        session.commit()

        version = session.scalar(select(SchemaVersion.version))
        assert version == CURRENT_SCHEMA_VERSION
