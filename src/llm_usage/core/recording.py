"""Recording usage events into the local database.

The events-writing counterpart to `pricing.py`. `record_event()` is the
single entry point for turning a described LLM call into a persisted
`usage_events` row, with cost snapshotted at write time. The
`record_usage` MCP tool calls it today; the capture proxy and SDK
wrappers will call it when they land — none of those paths should go
through the MCP layer to write an event.

Cost snapshotting: cost is looked up from `pricing_snapshot` and frozen
into the row at insert time. A pricing change later does not rewrite
history (event-sourcing principle). A model absent from the pricing
table is not an error — the call is still recorded, with `cost = 0` and
a warning.

Idempotency: when `request_id` is provided and a row with that
`request_id` already exists, `record_event` returns the existing row's
id and cost instead of inserting a duplicate — "won't double-count"
when replaying a log file. First write wins; the stored
`cost_nano_usd` is never recomputed.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from llm_usage.core.db.models import UsageEvent
from llm_usage.core.pricing import CostCalculator, get_pricing

# Surfaced verbatim at the MCP boundary as `RecordUsageResult.warning`.
# The pricing-missing wording matches the example in `docs/spec.md`.
_PRICING_MISSING_WARNING = "model not in pricing table; cost set to 0"
_DEDUP_WARNING = "request_id already recorded; returning the existing event"


@dataclass(frozen=True)
class RecordedEvent:
    """Outcome of a `record_event` call.

    `deduplicated` is True when `request_id` matched an existing row and
    no new row was inserted — `id` and `cost_nano_usd` are then the
    existing row's values. `warning` is a human-readable note for the
    MCP boundary (pricing-missing on insert, or dedup); `None` on a
    clean insert with known pricing.
    """

    id: str
    cost_nano_usd: int
    warning: str | None
    deduplicated: bool


def record_event(
    session: Session,
    *,
    provider: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_write_tokens: int = 0,
    cache_read_tokens: int = 0,
    duration_ms: int | None = None,
    success: bool = True,
    error_type: str | None = None,
    request_id: str | None = None,
    project: str | None = None,
    tags: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
    timestamp: int | None = None,
) -> RecordedEvent:
    """Persist one LLM call as a `usage_events` row; return its id + cost.

    Cost is looked up from `pricing_snapshot` and snapshotted into the
    row. A model absent from the pricing table is recorded with
    `cost = 0` and a warning rather than raising.

    `request_id`, when given, makes the call idempotent: a second call
    with the same `request_id` returns the existing row untouched.

    Does not commit — the caller owns the transaction, matching
    `upsert_pricing`'s convention. `timestamp` defaults to now (ms epoch).
    The internal rollback on a write race assumes `record_event` is the
    sole writer in the caller's transaction (true for the MCP tool).
    """
    # Fast path: a known request_id is an idempotent no-op insert.
    if request_id is not None:
        existing = _find_by_request_id(session, request_id)
        if existing is not None:
            return RecordedEvent(
                id=existing.id,
                cost_nano_usd=existing.cost_nano_usd,
                warning=_DEDUP_WARNING,
                deduplicated=True,
            )

    cost_nano_usd, warning = _compute_cost(
        session,
        provider=provider,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_write_tokens=cache_write_tokens,
        cache_read_tokens=cache_read_tokens,
    )

    event = UsageEvent(
        id=str(uuid.uuid4()),
        timestamp=timestamp if timestamp is not None else int(time.time() * 1000),
        provider=provider,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_write_tokens=cache_write_tokens,
        cache_read_tokens=cache_read_tokens,
        cost_nano_usd=cost_nano_usd,
        duration_ms=duration_ms,
        success=success,
        error_type=error_type,
        request_id=request_id,
        project=project,
        tags=json.dumps(tags) if tags is not None else None,
        event_metadata=json.dumps(metadata) if metadata is not None else None,
    )
    session.add(event)
    try:
        session.flush()
    except IntegrityError:
        # Race: another writer inserted the same request_id between our
        # pre-check and this flush. Roll back and return the row that won.
        session.rollback()
        if request_id is None:
            raise  # not a request_id conflict — surface it
        existing = _find_by_request_id(session, request_id)
        if existing is None:
            raise  # the conflict wasn't the request_id index — surface it
        return RecordedEvent(
            id=existing.id,
            cost_nano_usd=existing.cost_nano_usd,
            warning=_DEDUP_WARNING,
            deduplicated=True,
        )

    return RecordedEvent(
        id=event.id,
        cost_nano_usd=cost_nano_usd,
        warning=warning,
        deduplicated=False,
    )


def _find_by_request_id(session: Session, request_id: str) -> UsageEvent | None:
    stmt = select(UsageEvent).where(UsageEvent.request_id == request_id)
    return session.scalars(stmt).first()


def _compute_cost(
    session: Session,
    *,
    provider: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_write_tokens: int,
    cache_read_tokens: int,
) -> tuple[int, str | None]:
    """Return `(cost_nano_usd, warning)`. Missing pricing -> `(0, warning)`."""
    pricing = get_pricing(session, provider, model)
    if pricing is None:
        return 0, _PRICING_MISSING_WARNING
    cost = CostCalculator(pricing).cost_nano_usd(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_write_tokens=cache_write_tokens,
        cache_read_tokens=cache_read_tokens,
    )
    return cost, None
