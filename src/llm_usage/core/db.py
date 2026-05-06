"""SQLAlchemy models for the local usage database.

Schema mirrors `docs/spec.md` verbatim. Three tables:

- `usage_events`     one row per LLM call, with pre-computed `cost_usd`.
- `pricing_snapshot` materialized view of the vendored pricing JSON.
- `schema_version`   single-row table holding the active schema version.

Models are defined in SQLAlchemy 2.0 typed style and are sync/async-agnostic;
the engine and session factory live elsewhere.
"""

from __future__ import annotations

from sqlalchemy import (
    Boolean,
    Float,
    Index,
    Integer,
    Text,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

CURRENT_SCHEMA_VERSION = 1


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


class UsageEvent(Base):
    """One LLM API call. `cost_usd` is snapshotted at insert time."""

    __tablename__ = "usage_events"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    timestamp: Mapped[int] = mapped_column(Integer, nullable=False)
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    input_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    output_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    cache_write_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    cache_read_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    cost_usd: Mapped[float] = mapped_column(Float, nullable=False)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    success: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("1")
    )
    error_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    request_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    project: Mapped[str | None] = mapped_column(Text, nullable=True)
    tags: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Column is named `metadata` in the spec; SQLAlchemy reserves `Base.metadata`,
    # so the Python attribute is `event_metadata` while the column name stays `metadata`.
    event_metadata: Mapped[str | None] = mapped_column("metadata", Text, nullable=True)

    __table_args__ = (
        Index("idx_events_timestamp", "timestamp"),
        Index("idx_events_provider_model", "provider", "model"),
        Index("idx_events_project", "project"),
        Index(
            "idx_events_request_id",
            "request_id",
            unique=True,
            sqlite_where=text("request_id IS NOT NULL"),
        ),
    )


class PricingSnapshot(Base):
    """Materialized pricing per (provider, model) at fetch time."""

    __tablename__ = "pricing_snapshot"

    provider: Mapped[str] = mapped_column(Text, primary_key=True)
    model: Mapped[str] = mapped_column(Text, primary_key=True)
    input_per_million_usd: Mapped[float] = mapped_column(Float, nullable=False)
    output_per_million_usd: Mapped[float] = mapped_column(Float, nullable=False)
    cache_write_per_million_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    cache_read_per_million_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    fetched_at: Mapped[int] = mapped_column(Integer, nullable=False)


class SchemaVersion(Base):
    """Single-row table tracking the active schema version."""

    __tablename__ = "schema_version"

    version: Mapped[int] = mapped_column(Integer, primary_key=True)
