"""Tests for the `usage_summary` MCP tool + its calendar-period helpers.

Both the public tool and the load-bearing private helpers (`period_window`,
`parse_iso_to_ms`) are exercised here. Period boundaries are math-on-
strings, so they get unit tests independent of the DB.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest

import llm_usage.mcp.server as server_module
from llm_usage.bootstrap import migrate_to_head
from llm_usage.core.db.session import get_session
from llm_usage.core.models import Period, UsageSummaryResult
from llm_usage.core.pricing import Pricing, upsert_pricing
from llm_usage.core.recording import record_event
from llm_usage.core.spend import (
    parse_iso_to_ms,
    period_window,
    summarize_usage,
)

# Anchor: Friday 2026-05-15 12:00:00 UTC.
_T0_DT = datetime(2026, 5, 15, 12, 0, 0, tzinfo=UTC)
_T0_MS = int(_T0_DT.timestamp() * 1000)
_DAY = 86_400_000


# --- period_window ---------------------------------------------------------


def test_period_window_today_floors_to_midnight() -> None:
    start_ms, end_ms = period_window("today", _T0_MS)
    # Midnight UTC on 2026-05-15.
    assert datetime.fromtimestamp(start_ms / 1000, tz=UTC) == datetime(2026, 5, 15, tzinfo=UTC)
    assert end_ms == _T0_MS


def test_period_window_week_floors_to_monday() -> None:
    """_T0 is Friday 2026-05-15; Monday is 2026-05-11."""
    start_ms, _ = period_window("week", _T0_MS)
    expected = datetime(2026, 5, 11, tzinfo=UTC)
    assert datetime.fromtimestamp(start_ms / 1000, tz=UTC) == expected


def test_period_window_week_on_a_monday_is_that_monday() -> None:
    """A `now` that's exactly Monday should produce that Monday's 00:00."""
    monday = datetime(2026, 5, 18, 9, 0, 0, tzinfo=UTC)  # Mon 09:00
    start_ms, _ = period_window("week", int(monday.timestamp() * 1000))
    assert datetime.fromtimestamp(start_ms / 1000, tz=UTC) == datetime(2026, 5, 18, tzinfo=UTC)


def test_period_window_month_floors_to_first(_t0: int = _T0_MS) -> None:
    start_ms, _ = period_window("month", _t0)
    assert datetime.fromtimestamp(start_ms / 1000, tz=UTC) == datetime(2026, 5, 1, tzinfo=UTC)


def test_period_window_year_floors_to_jan_1() -> None:
    start_ms, _ = period_window("year", _T0_MS)
    assert datetime.fromtimestamp(start_ms / 1000, tz=UTC) == datetime(2026, 1, 1, tzinfo=UTC)


# --- parse_iso_to_ms -------------------------------------------------------


def test_parse_iso_accepts_z_suffix() -> None:
    assert parse_iso_to_ms("2026-05-15T12:00:00Z") == _T0_MS


def test_parse_iso_accepts_explicit_offset() -> None:
    assert parse_iso_to_ms("2026-05-15T12:00:00+00:00") == _T0_MS


def test_parse_iso_naive_treated_as_utc() -> None:
    """A naive timestamp must not depend on the host's local zone."""
    assert parse_iso_to_ms("2026-05-15T12:00:00") == _T0_MS


def test_parse_iso_date_only_is_midnight_utc() -> None:
    assert parse_iso_to_ms("2026-05-15") == int(
        datetime(2026, 5, 15, tzinfo=UTC).timestamp() * 1000
    )


# --- usage_summary (DB-backed) --------------------------------------------


@pytest.fixture
def seeded_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db = tmp_path / "usage.db"
    monkeypatch.setenv("LLM_USAGE_DB_URL", f"sqlite:///{db}")
    migrate_to_head()
    with get_session() as session:
        upsert_pricing(
            session,
            [
                Pricing("openai", "gpt-test", 1.0, 2.0, fetched_at=1),
                Pricing("anthropic", "claude-test", 2.0, 4.0, fetched_at=1),
                Pricing("deepseek", "ds-test", 0.5, 1.0, fetched_at=1),
                Pricing("qwen", "qwen-test", 0.25, 0.5, fetched_at=1),
            ],
        )
        # All four events on 2026-05-15 (today, in the test's frame).
        record_event(
            session,
            provider="anthropic",
            model="claude-test",
            input_tokens=1_000_000,
            output_tokens=1_000_000,
            timestamp=_T0_MS - 60_000,
        )  # $6 — the largest call
        record_event(
            session,
            provider="openai",
            model="gpt-test",
            input_tokens=1_000_000,
            output_tokens=1_000_000,
            timestamp=_T0_MS - 120_000,
        )  # $3
        record_event(
            session,
            provider="deepseek",
            model="ds-test",
            input_tokens=1_000_000,
            output_tokens=1_000_000,
            timestamp=_T0_MS - 180_000,
        )  # $1.5
        record_event(
            session,
            provider="qwen",
            model="qwen-test",
            input_tokens=1_000_000,
            output_tokens=1_000_000,
            timestamp=_T0_MS - 240_000,
        )  # $0.75
        session.commit()
    return db


@pytest.fixture
def empty_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db = tmp_path / "usage.db"
    monkeypatch.setenv("LLM_USAGE_DB_URL", f"sqlite:///{db}")
    migrate_to_head()
    return db


def _summary(period: Period = "week", *, now_ms: int = _T0_MS) -> UsageSummaryResult:
    """Bypass the MCP wrapper's wall-clock `time.time()` for determinism."""
    with get_session() as session:
        return summarize_usage(session, period=period, now_ms=now_ms)


def test_summary_totals_match_seeded_data(seeded_db: Path) -> None:
    r = _summary("today")
    assert r.total_cost_usd == pytest.approx(11.25)  # $6 + $3 + $1.5 + $0.75
    assert r.call_count == 4
    assert r.period == "today"


def test_summary_top_providers_capped_at_three_and_cost_desc(seeded_db: Path) -> None:
    """Four providers seeded; top_providers caps at 3."""
    r = _summary("today")
    assert len(r.top_providers) == 3
    names = [p.provider for p in r.top_providers]
    assert names == ["anthropic", "openai", "deepseek"]
    # `pct` is each group's share of the $11.25 total.
    assert r.top_providers[0].pct == round(6.0 / 11.25 * 100, 2)
    assert r.top_providers[1].pct == round(3.0 / 11.25 * 100, 2)


def test_summary_top_models_capped_at_three(seeded_db: Path) -> None:
    r = _summary("today")
    assert len(r.top_models) == 3
    assert [m.model for m in r.top_models] == ["claude-test", "gpt-test", "ds-test"]


def test_summary_largest_call_is_the_highest_cost_event(seeded_db: Path) -> None:
    r = _summary("today")
    assert r.largest_call is not None
    assert r.largest_call.model == "claude-test"
    assert r.largest_call.cost_usd == pytest.approx(6.0)
    assert r.largest_call.timestamp == _T0_MS - 60_000


def test_summary_empty_db_has_null_largest_call(empty_db: Path) -> None:
    r = _summary("week")
    assert r.total_cost_usd == 0.0
    assert r.call_count == 0
    assert r.top_providers == []
    assert r.top_models == []
    assert r.largest_call is None


def test_summary_window_excludes_events_outside_period(seeded_db: Path) -> None:
    """Pull `now` forward by 8 days so the 'today' window catches nothing."""
    r = _summary("today", now_ms=_T0_MS + 8 * _DAY)
    assert r.call_count == 0
    assert r.largest_call is None


# --- MCP wrapper -----------------------------------------------------------


def test_mcp_tool_default_period_is_week(seeded_db: Path) -> None:
    """The MCP wrapper defaults `period` to 'week' per the spec."""
    r: UsageSummaryResult = asyncio.run(server_module.usage_summary())
    # The wrapper uses wall-clock now; we can't pin events into "this
    # week" deterministically, but the call must succeed and `period`
    # round-trips.
    assert r.period == "week"
