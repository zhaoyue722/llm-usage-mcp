"""Tests for `llm_usage.core.recording` and the `record_usage` MCP tool.

Two layers:

- `record_event()` (core) — cost snapshotting, missing-pricing warning,
  `request_id` idempotency, JSON serialization of tags/metadata,
  timestamp handling. Tested against a controlled pricing row inserted
  by the test so cost math is exact.
- `record_usage` (MCP tool) — thin wrapper; tested for the wiring
  (returns `RecordUsageResult` with float `cost_usd`) and the
  record-then-read round trip through `usage://recent_events`.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

import llm_usage.mcp.server as server_module
from llm_usage.bootstrap import migrate_to_head
from llm_usage.core.db.models import UsageEvent
from llm_usage.core.db.session import get_session
from llm_usage.core.pricing import Pricing, Tier, nano_to_usd, upsert_pricing
from llm_usage.core.recording import record_event

# A controlled pricing row with round numbers so cost math is exact:
# $2/M input, $6/M output, $2.50/M cache-write, $0.20/M cache-read.
_TEST_PRICING = Pricing(
    provider="anthropic",
    model="claude-test-1",
    input_per_million_usd=2.0,
    output_per_million_usd=6.0,
    cache_write_per_million_usd=2.5,
    cache_read_per_million_usd=0.2,
    fetched_at=1_700_000_000_000,
)


@pytest.fixture
def priced_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Fresh DB with the schema and exactly one controlled pricing row."""
    db = tmp_path / "usage.db"
    monkeypatch.setenv("LLM_USAGE_DB_URL", f"sqlite:///{db}")
    migrate_to_head()
    with get_session() as session:
        upsert_pricing(session, [_TEST_PRICING])
        session.commit()
    return db


def _count_events() -> int:
    with get_session() as session:
        return session.scalar(select(func.count()).select_from(UsageEvent)) or 0


def _get_event(event_id: str) -> UsageEvent | None:
    with get_session() as session:
        return session.get(UsageEvent, event_id)


# --- record_event: cost ----------------------------------------------------


def test_record_event_computes_cost_from_pricing(priced_db: Path) -> None:
    with get_session() as session:
        recorded = record_event(
            session,
            provider="anthropic",
            model="claude-test-1",
            input_tokens=1_000_000,
            output_tokens=1_000_000,
        )
        session.commit()
    # 1M input @ $2/M + 1M output @ $6/M = $8.00 = 8e9 nano-USD.
    assert recorded.cost_nano_usd == 8_000_000_000
    assert recorded.warning is None
    assert recorded.deduplicated is False


def test_record_event_includes_cache_token_cost(priced_db: Path) -> None:
    with get_session() as session:
        recorded = record_event(
            session,
            provider="anthropic",
            model="claude-test-1",
            input_tokens=0,
            output_tokens=0,
            cache_write_tokens=1_000_000,
            cache_read_tokens=1_000_000,
        )
        session.commit()
    # 1M cache-write @ $2.50/M + 1M cache-read @ $0.20/M = $2.70 = 2.7e9 nano.
    assert recorded.cost_nano_usd == 2_700_000_000


def test_record_event_missing_pricing_records_zero_cost_with_warning(priced_db: Path) -> None:
    with get_session() as session:
        recorded = record_event(
            session,
            provider="anthropic",
            model="model-not-in-table",
            input_tokens=500,
            output_tokens=500,
        )
        session.commit()
    assert recorded.cost_nano_usd == 0
    assert recorded.warning == "model not in pricing table; cost set to 0"
    # The event is still recorded — missing pricing is not a failure.
    assert _count_events() == 1
    stored = _get_event(recorded.id)
    assert stored is not None
    assert stored.cost_nano_usd == 0


# --- record_event: persistence ---------------------------------------------


def test_record_event_persists_all_fields(priced_db: Path) -> None:
    with get_session() as session:
        recorded = record_event(
            session,
            provider="anthropic",
            model="claude-test-1",
            input_tokens=10,
            output_tokens=20,
            cache_write_tokens=3,
            cache_read_tokens=4,
            duration_ms=1234,
            success=False,
            error_type="rate_limit",
            project="my-project",
            timestamp=1_700_000_000_000,
        )
        session.commit()

    stored = _get_event(recorded.id)
    assert stored is not None
    assert stored.provider == "anthropic"
    assert stored.model == "claude-test-1"
    assert stored.input_tokens == 10
    assert stored.output_tokens == 20
    assert stored.cache_write_tokens == 3
    assert stored.cache_read_tokens == 4
    assert stored.duration_ms == 1234
    assert stored.success is False
    assert stored.error_type == "rate_limit"
    assert stored.project == "my-project"
    assert stored.timestamp == 1_700_000_000_000


def test_record_event_serializes_tags_and_metadata_as_json(priced_db: Path) -> None:
    with get_session() as session:
        recorded = record_event(
            session,
            provider="anthropic",
            model="claude-test-1",
            input_tokens=1,
            output_tokens=1,
            tags=["prod", "billing"],
            metadata={"trace_id": "abc-123", "retries": 2},
        )
        session.commit()

    stored = _get_event(recorded.id)
    assert stored is not None
    assert stored.tags is not None and stored.event_metadata is not None
    # Columns hold JSON-encoded text; parsing round-trips the originals.
    assert json.loads(stored.tags) == ["prod", "billing"]
    assert json.loads(stored.event_metadata) == {"trace_id": "abc-123", "retries": 2}


def test_record_event_null_tags_and_metadata_stay_null(priced_db: Path) -> None:
    with get_session() as session:
        recorded = record_event(
            session,
            provider="anthropic",
            model="claude-test-1",
            input_tokens=1,
            output_tokens=1,
        )
        session.commit()
    stored = _get_event(recorded.id)
    assert stored is not None
    assert stored.tags is None
    assert stored.event_metadata is None


def test_record_event_defaults_timestamp_to_now(priced_db: Path) -> None:
    before = int(time.time() * 1000)
    with get_session() as session:
        recorded = record_event(
            session,
            provider="anthropic",
            model="claude-test-1",
            input_tokens=1,
            output_tokens=1,
        )
        session.commit()
    after = int(time.time() * 1000)
    stored = _get_event(recorded.id)
    assert stored is not None
    assert before <= stored.timestamp <= after


def test_record_event_generates_unique_ids(priced_db: Path) -> None:
    with get_session() as session:
        first = record_event(
            session,
            provider="anthropic",
            model="claude-test-1",
            input_tokens=1,
            output_tokens=1,
        )
        second = record_event(
            session,
            provider="anthropic",
            model="claude-test-1",
            input_tokens=1,
            output_tokens=1,
        )
        session.commit()
    assert first.id != second.id
    assert _count_events() == 2


# --- record_event: request_id idempotency ----------------------------------


def test_record_event_new_request_id_inserts(priced_db: Path) -> None:
    with get_session() as session:
        recorded = record_event(
            session,
            provider="anthropic",
            model="claude-test-1",
            input_tokens=1,
            output_tokens=1,
            request_id="req-1",
        )
        session.commit()
    assert recorded.deduplicated is False
    assert recorded.warning is None
    assert _count_events() == 1


def test_record_event_duplicate_request_id_returns_existing(priced_db: Path) -> None:
    with get_session() as session:
        first = record_event(
            session,
            provider="anthropic",
            model="claude-test-1",
            input_tokens=1_000_000,
            output_tokens=0,
            request_id="req-dup",
        )
        session.commit()

    with get_session() as session:
        second = record_event(
            session,
            provider="anthropic",
            model="claude-test-1",
            # Different token counts on the replay — must be ignored.
            input_tokens=999,
            output_tokens=999,
            request_id="req-dup",
        )
        session.commit()

    assert second.deduplicated is True
    assert second.id == first.id
    # First write wins: the stored cost is the original, not recomputed.
    assert second.cost_nano_usd == first.cost_nano_usd == 2_000_000_000
    assert second.warning == "request_id already recorded; returning the existing event"
    # No duplicate row.
    assert _count_events() == 1


def test_record_event_null_request_id_never_deduplicates(priced_db: Path) -> None:
    """Two calls with no request_id are two distinct events."""
    with get_session() as session:
        record_event(
            session,
            provider="anthropic",
            model="claude-test-1",
            input_tokens=1,
            output_tokens=1,
            request_id=None,
        )
        record_event(
            session,
            provider="anthropic",
            model="claude-test-1",
            input_tokens=1,
            output_tokens=1,
            request_id=None,
        )
        session.commit()
    assert _count_events() == 2


def test_record_event_write_race_returns_the_winning_row(
    priced_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A request_id inserted between the pre-check and the flush is handled.

    The pre-check can't see a row that another writer commits a moment
    later, so the flush hits the partial UNIQUE index. `record_event`
    must catch the `IntegrityError`, roll back, and return the row that
    won — not raise. Simulated deterministically by forcing the
    pre-check lookup to "miss" exactly once while the row really exists.
    """
    from llm_usage.core import recording

    with get_session() as session:
        winner = record_event(
            session,
            provider="anthropic",
            model="claude-test-1",
            input_tokens=1_000_000,
            output_tokens=0,
            request_id="race",
        )
        session.commit()

    real_lookup = recording._find_by_request_id
    call_count = {"n": 0}

    def flaky_lookup(session: Session, request_id: str) -> UsageEvent | None:
        call_count["n"] += 1
        if call_count["n"] == 1:
            return None  # pre-check "misses" — simulates the race window
        return real_lookup(session, request_id)  # except-block re-query finds it

    monkeypatch.setattr(recording, "_find_by_request_id", flaky_lookup)

    with get_session() as session:
        recorded = record_event(
            session,
            provider="anthropic",
            model="claude-test-1",
            input_tokens=999,
            output_tokens=999,
            request_id="race",
        )
        session.commit()

    assert recorded.deduplicated is True
    assert recorded.id == winner.id
    assert recorded.cost_nano_usd == winner.cost_nano_usd
    assert call_count["n"] == 2  # pre-check miss, then except-block re-query
    assert _count_events() == 1  # no duplicate row


# --- record_usage MCP tool -------------------------------------------------


def test_record_usage_tool_returns_float_cost_usd(priced_db: Path) -> None:
    result = asyncio.run(
        server_module.record_usage(
            provider="anthropic",
            model="claude-test-1",
            input_tokens=1_000_000,
            output_tokens=0,
        )
    )
    # 1M input @ $2/M = $2.00.
    assert result.cost_usd == pytest.approx(2.0)
    assert result.warning is None
    assert isinstance(result.id, str) and result.id


def test_record_usage_tool_missing_pricing_warning(priced_db: Path) -> None:
    result = asyncio.run(
        server_module.record_usage(
            provider="anthropic",
            model="unpriced-model",
            input_tokens=100,
            output_tokens=100,
        )
    )
    assert result.cost_usd == 0.0
    assert result.warning == "model not in pricing table; cost set to 0"


def test_record_usage_tool_round_trips_through_recent_events(priced_db: Path) -> None:
    """record_usage writes; usage://recent_events reads its own write back."""
    result = asyncio.run(
        server_module.record_usage(
            provider="anthropic",
            model="claude-test-1",
            input_tokens=500_000,
            output_tokens=250_000,
            project="round-trip",
            tags=["demo"],
        )
    )

    events = json.loads(server_module.recent_events())
    assert len(events) == 1
    event = events[0]
    assert event["id"] == result.id
    assert event["provider"] == "anthropic"
    assert event["model"] == "claude-test-1"
    assert event["project"] == "round-trip"
    assert event["tags"] == ["demo"]
    # 500k input @ $2/M + 250k output @ $6/M = $1.00 + $1.50 = $2.50.
    assert event["cost_usd"] == pytest.approx(2.5)
    assert event["cost_nano_usd"] == 2_500_000_000
    assert event["cost_usd"] == nano_to_usd(event["cost_nano_usd"])


def test_record_usage_tool_idempotent_on_request_id(priced_db: Path) -> None:
    first = asyncio.run(
        server_module.record_usage(
            provider="anthropic",
            model="claude-test-1",
            input_tokens=1,
            output_tokens=1,
            request_id="tool-req-1",
        )
    )
    second = asyncio.run(
        server_module.record_usage(
            provider="anthropic",
            model="claude-test-1",
            input_tokens=1,
            output_tokens=1,
            request_id="tool-req-1",
        )
    )
    assert second.id == first.id
    assert second.warning == "request_id already recorded; returning the existing event"
    # One row, despite two tool calls.
    assert len(json.loads(server_module.recent_events())) == 1


# --- tier-aware recording -------------------------------------------------
#
# Tiered pricing is set up by the loader via `upsert_pricing` (which
# also writes `pricing_tier` rows). The recorder reads via `get_pricing`
# (which loads tiers) and passes the resulting `Pricing` to
# `CostCalculator`, which now picks the right tier by `input_tokens`.
# These tests pin the end-to-end contract: same model, two prompt sizes
# in different tiers, two different recorded costs.

_QWEN_TIERED = Pricing(
    provider="qwen",
    model="qwen-flash-test",
    # Flat fallback = tier 0's rates per the loader's convention.
    input_per_million_usd=0.05,
    output_per_million_usd=0.40,
    fetched_at=1_700_000_000_000,
    tiers=(
        Tier(0, 0, 256_000, 0.05, 0.40),
        Tier(1, 256_000, 1_000_000, 0.25, 2.00),
    ),
)


@pytest.fixture
def tiered_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Fresh DB with the qwen-flash-shaped tiered pricing row."""
    db = tmp_path / "usage.db"
    monkeypatch.setenv("LLM_USAGE_DB_URL", f"sqlite:///{db}")
    migrate_to_head()
    with get_session() as session:
        upsert_pricing(session, [_QWEN_TIERED])
        session.commit()
    return db


def test_record_event_picks_tier_0_for_small_prompt(tiered_db: Path) -> None:
    """A 100k-input call falls in tier 0 → tier-0 rate at insert."""
    with get_session() as session:
        recorded = record_event(
            session,
            provider="qwen",
            model="qwen-flash-test",
            input_tokens=100_000,
            output_tokens=0,
        )
        session.commit()
    # 100k * $0.05/M = $0.005 = 5_000_000 nano.
    assert recorded.cost_nano_usd == 5_000_000


def test_record_event_picks_tier_1_for_large_prompt(tiered_db: Path) -> None:
    """A 500k-input call falls in tier 1 → tier-1 rate at insert.

    This is the bug PR2 fixes. Under PR1's behavior this same call
    would have recorded at tier 0's $0.05/M ($0.025 = 25_000_000
    nano), a 5x under-count.
    """
    with get_session() as session:
        recorded = record_event(
            session,
            provider="qwen",
            model="qwen-flash-test",
            input_tokens=500_000,
            output_tokens=0,
        )
        session.commit()
    # 500k * $0.25/M = $0.125 = 125_000_000 nano. Five times what
    # PR1's flat-only path would have produced.
    assert recorded.cost_nano_usd == 125_000_000


def test_record_event_same_model_two_sizes_get_two_costs(tiered_db: Path) -> None:
    """Two calls to the same model at different prompt sizes get
    different costs — proves the tier pick is per-call, not per-model."""
    with get_session() as session:
        small = record_event(
            session,
            provider="qwen",
            model="qwen-flash-test",
            input_tokens=10_000,
            output_tokens=0,
        )
        large = record_event(
            session,
            provider="qwen",
            model="qwen-flash-test",
            input_tokens=500_000,
            output_tokens=0,
        )
        session.commit()
    # 10k * $0.05/M = $5e-4 = 500_000 nano.
    assert small.cost_nano_usd == 500_000
    assert large.cost_nano_usd == 125_000_000  # 500k * $0.25/M


def test_record_event_at_tier_boundary_uses_higher_tier(tiered_db: Path) -> None:
    """At input_tokens=256_000 exactly, the recorder uses tier 1 (the
    half-open `[start, end)` convention)."""
    with get_session() as session:
        recorded = record_event(
            session,
            provider="qwen",
            model="qwen-flash-test",
            input_tokens=256_000,
            output_tokens=0,
        )
        session.commit()
    # 256k * $0.25/M (tier 1) = $0.064 = 64_000_000 nano.
    assert recorded.cost_nano_usd == 64_000_000
