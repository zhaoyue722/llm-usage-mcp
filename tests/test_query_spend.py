"""Tests for the `query_spend` MCP tool.

Seeds a fresh DB with deterministic timestamps and round-number rates
so totals and per-group rollups can be asserted by exact dollars.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest

import llm_usage.mcp.server as server_module
from llm_usage.bootstrap import migrate_to_head
from llm_usage.core.db.session import get_session
from llm_usage.core.models import GroupBy, QuerySpendResult, SpendFilter
from llm_usage.core.pricing import Pricing, upsert_pricing
from llm_usage.core.recording import record_event
from llm_usage.core.spend import aggregate_spend

# Anchor every seeded timestamp at a fixed wall-clock moment so window
# math is deterministic. 2026-05-15 12:00:00 UTC.
_T0_MS = int(datetime(2026, 5, 15, 12, 0, 0, tzinfo=UTC).timestamp() * 1000)
_DAY = 86_400_000

# Default window passed to `_query` covers every seeded event and is
# wall-clock-independent. Tests that care about window math override.
_SEED_START = "2026-05-01T00:00:00Z"
_SEED_END = "2026-05-16T00:00:00Z"


@pytest.fixture
def seeded_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Pricing + four events spanning two days, two providers, two projects."""
    db = tmp_path / "usage.db"
    monkeypatch.setenv("LLM_USAGE_DB_URL", f"sqlite:///{db}")
    migrate_to_head()
    with get_session() as session:
        upsert_pricing(
            session,
            [
                # $1/M in, $2/M out — at 1M/1M tokens => $3 / call
                Pricing("openai", "gpt-test", 1.0, 2.0, fetched_at=1),
                # $2/M in, $4/M out — at 1M/1M tokens => $6 / call
                Pricing("anthropic", "claude-test", 2.0, 4.0, fetched_at=1),
            ],
        )
        # Two days ago: two events, both project="alpha".
        record_event(
            session,
            provider="openai",
            model="gpt-test",
            input_tokens=1_000_000,
            output_tokens=1_000_000,
            project="alpha",
            tags=["prod", "us-east"],
            timestamp=_T0_MS - 2 * _DAY,
        )  # $3
        record_event(
            session,
            provider="anthropic",
            model="claude-test",
            input_tokens=1_000_000,
            output_tokens=1_000_000,
            project="alpha",
            tags=["prod"],
            timestamp=_T0_MS - 2 * _DAY,
        )  # $6
        # One day ago: project="beta", tagged.
        record_event(
            session,
            provider="openai",
            model="gpt-test",
            input_tokens=500_000,
            output_tokens=500_000,
            project="beta",
            tags=["experimental"],
            timestamp=_T0_MS - _DAY,
        )  # $1.5
        # Today: untagged, no project.
        record_event(
            session,
            provider="openai",
            model="gpt-test",
            input_tokens=500_000,
            output_tokens=500_000,
            timestamp=_T0_MS,
        )  # $1.5
        session.commit()
    return db


@pytest.fixture
def empty_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db = tmp_path / "usage.db"
    monkeypatch.setenv("LLM_USAGE_DB_URL", f"sqlite:///{db}")
    migrate_to_head()
    return db


def _query(
    *,
    start: str | None = _SEED_START,
    end: str | None = _SEED_END,
    group_by: GroupBy = "provider",
    filter: SpendFilter | None = None,
    include_failed: bool = False,
) -> QuerySpendResult:
    """Call `query_spend` with an explicit window that covers seeded events.

    The MCP tool's default window is "30 days ago to wall-clock now",
    which is not deterministic for fixed-timestamp test data. Pass
    `start=None`/`end=None` to exercise the defaults via the dedicated
    test below.
    """
    result: QuerySpendResult = asyncio.run(
        server_module.query_spend(
            start=start,
            end=end,
            group_by=group_by,
            filter=filter,
            include_failed=include_failed,
        )
    )
    return result


# --- totals ----------------------------------------------------------------


def test_totals_cover_every_event_in_explicit_window(seeded_db: Path) -> None:
    """Window covers every seeded event."""
    r = _query()
    assert r.total_cost_usd == pytest.approx(12.0)  # $3 + $6 + $1.5 + $1.5
    assert r.total_calls == 4
    assert r.total_input_tokens == 3_000_000
    assert r.total_output_tokens == 3_000_000


def test_default_window_is_30_days_back_from_now_ms(seeded_db: Path) -> None:
    """Hit the core helper directly so `now_ms` is deterministic.

    Pin `now_ms` to the seed anchor; the default `end` then becomes
    `_T0_MS`, and the default `start` becomes `_T0_MS - 30 days`. All
    seeded events sit inside that window.
    """
    with get_session() as session:
        r = aggregate_spend(
            session, start_ms=None, end_ms=None, group_by="provider", filter=None, now_ms=_T0_MS
        )
    # Half-open at `end`: the today event (timestamp == _T0_MS exactly)
    # is excluded; three of the four events remain.
    assert r.total_calls == 3
    assert r.total_cost_usd == pytest.approx(10.5)  # $3 + $6 + $1.5


def test_totals_zero_on_empty_window(seeded_db: Path) -> None:
    """A window with no events: ints stay ints (no NULL/None leak)."""
    r = _query(start="2030-01-01", end="2030-01-02")
    assert r.total_cost_usd == 0.0
    assert r.total_calls == 0
    assert r.total_input_tokens == 0
    assert r.total_output_tokens == 0
    assert r.groups == []


def test_totals_zero_on_empty_db(empty_db: Path) -> None:
    r = _query()
    assert r.total_cost_usd == 0.0
    assert r.total_calls == 0
    assert r.groups == []


# --- group_by --------------------------------------------------------------


def test_group_by_provider(seeded_db: Path) -> None:
    """openai has 3 events ($3+$1.5+$1.5=$6), anthropic has 1 ($6)."""
    r = _query(group_by="provider")
    by_key = {g.key: g for g in r.groups}
    assert set(by_key) == {"openai", "anthropic"}
    assert by_key["openai"].cost_usd == pytest.approx(6.0)
    assert by_key["openai"].calls == 3
    assert by_key["anthropic"].cost_usd == pytest.approx(6.0)
    assert by_key["anthropic"].calls == 1
    # Cost-desc; equal costs break alphabetically -> anthropic before openai.
    assert [g.key for g in r.groups] == ["anthropic", "openai"]


def test_group_by_model(seeded_db: Path) -> None:
    r = _query(group_by="model")
    keys = {g.key for g in r.groups}
    assert keys == {"gpt-test", "claude-test"}


def test_group_by_project_drops_null_project(seeded_db: Path) -> None:
    """The today-event has no project; it must not surface as a group."""
    r = _query(group_by="project")
    keys = [g.key for g in r.groups]
    assert keys == ["alpha", "beta"]  # cost-desc: alpha=$9, beta=$1.5
    by_key = {g.key: g for g in r.groups}
    assert by_key["alpha"].cost_usd == pytest.approx(9.0)
    assert by_key["alpha"].calls == 2
    assert by_key["beta"].cost_usd == pytest.approx(1.5)


def test_group_by_day_uses_iso_date_keys(seeded_db: Path) -> None:
    r = _query(group_by="day")
    keys = {g.key for g in r.groups}
    assert keys == {"2026-05-13", "2026-05-14", "2026-05-15"}
    by_key = {g.key: g for g in r.groups}
    assert by_key["2026-05-13"].cost_usd == pytest.approx(9.0)  # $3 + $6
    assert by_key["2026-05-14"].cost_usd == pytest.approx(1.5)
    assert by_key["2026-05-15"].cost_usd == pytest.approx(1.5)


# --- group_by="tag" (Option A semantics) ----------------------------------


def test_group_by_tag_excludes_untagged_event(seeded_db: Path) -> None:
    """The today-event is untagged; its $1.5 must not appear in any tag group."""
    r = _query(group_by="tag")
    keys = {g.key for g in r.groups}
    assert keys == {"prod", "us-east", "experimental"}
    by_key = {g.key: g for g in r.groups}
    # `prod` covers both 2-days-ago events ($3 + $6 = $9).
    assert by_key["prod"].cost_usd == pytest.approx(9.0)
    assert by_key["prod"].calls == 2
    # `us-east` only on one of those events ($3).
    assert by_key["us-east"].cost_usd == pytest.approx(3.0)
    assert by_key["us-east"].calls == 1
    # `experimental` is the lone-tag yesterday event ($1.5).
    assert by_key["experimental"].cost_usd == pytest.approx(1.5)


def test_group_by_tag_per_group_calls_can_exceed_total(seeded_db: Path) -> None:
    """Multi-tag rows are counted once per tag; per-group sums may exceed total."""
    r = _query(group_by="tag")
    total_tagged_calls = sum(g.calls for g in r.groups)
    # prod=2, us-east=1, experimental=1 -> 4; total tagged events is only 3.
    assert total_tagged_calls == 4
    # The total_calls field still reflects every event in the window.
    assert r.total_calls == 4  # includes the untagged event


# --- filter ----------------------------------------------------------------


def test_filter_provider_restricts(seeded_db: Path) -> None:
    r = _query(filter=SpendFilter(provider="openai"))
    assert r.total_cost_usd == pytest.approx(6.0)
    assert r.total_calls == 3


def test_filter_model_restricts(seeded_db: Path) -> None:
    r = _query(filter=SpendFilter(model="claude-test"))
    assert r.total_cost_usd == pytest.approx(6.0)
    assert r.total_calls == 1


def test_filter_project_restricts(seeded_db: Path) -> None:
    r = _query(filter=SpendFilter(project="alpha"))
    assert r.total_cost_usd == pytest.approx(9.0)
    assert r.total_calls == 2


def test_filter_combines_with_and(seeded_db: Path) -> None:
    """All three filter axes are AND-combined."""
    r = _query(filter=SpendFilter(provider="openai", project="alpha"))
    # Only the 2-days-ago openai event has project=alpha.
    assert r.total_cost_usd == pytest.approx(3.0)
    assert r.total_calls == 1


# --- window ----------------------------------------------------------------


def test_window_is_half_open_at_end(seeded_db: Path) -> None:
    """`end` is exclusive: an event exactly at `end_ms` is *not* counted."""
    # _T0_MS is the timestamp of the today-event. Setting end to that
    # exact moment must exclude it.
    end_iso = "2026-05-15T12:00:00Z"
    r = _query(start="2026-05-15", end=end_iso)
    # Only events strictly before 12:00 on 2026-05-15 should be in. The
    # today-event is at exactly 12:00 -> excluded; yesterday's event is
    # outside [start, end) because its day is 2026-05-14.
    # On 2026-05-15 before 12:00, there are no events.
    assert r.total_calls == 0


def test_window_iso_with_z_suffix(seeded_db: Path) -> None:
    """ISO-8601 trailing-`Z` is accepted (delegates to `parse_iso_to_ms`)."""
    r = _query(start="2026-05-14T00:00:00Z", end="2026-05-16T00:00:00Z")
    # Yesterday + today: $1.5 + $1.5 = $3
    assert r.total_cost_usd == pytest.approx(3.0)
    assert r.total_calls == 2


def test_window_naive_iso_treated_as_utc(seeded_db: Path) -> None:
    """A naive ISO-8601 string is interpreted as UTC, not local time."""
    r = _query(start="2026-05-14T00:00:00", end="2026-05-16T00:00:00")
    assert r.total_calls == 2


# --- include_failed --------------------------------------------------------


@pytest.fixture
def seeded_db_with_failure(seeded_db: Path) -> Path:
    """Seeded baseline + one failure row from a streamed call.

    Mirrors what the streaming proxy writes on a mid-flight failure:
    `success=False`, partial counts from the last `message_delta`
    observed (here: input=2_000_000, output=0), `error_type` set,
    `request_id=None`. Cost is computed normally — the streaming
    recorder doesn't fabricate the cost number; if the call accrued
    1M input tokens at $1/M and 0 output, cost is $2.
    """
    with get_session() as session:
        record_event(
            session,
            provider="openai",
            model="gpt-test",
            input_tokens=2_000_000,
            output_tokens=0,
            success=False,
            error_type="stream_interrupted",
            timestamp=_T0_MS - 3 * 3600 * 1000,  # 3h before T0
        )
        session.commit()
    return seeded_db


def test_default_excludes_failure_rows_from_totals(seeded_db_with_failure: Path) -> None:
    """No include_failed=True -> failure row drops out of totals."""
    r = _query()
    # Same as the baseline test: 4 success rows summing to $12.
    assert r.total_cost_usd == pytest.approx(12.0)
    assert r.total_calls == 4
    assert r.total_input_tokens == 3_000_000


def test_include_failed_true_folds_failure_rows_in(seeded_db_with_failure: Path) -> None:
    """Opt-in: failure row contributes to totals + groups."""
    r = _query(include_failed=True)
    # Baseline $12 + failure $2 = $14; 5 calls; input tokens +2M.
    assert r.total_cost_usd == pytest.approx(14.0)
    assert r.total_calls == 5
    assert r.total_input_tokens == 5_000_000


def test_include_failed_propagates_to_grouped_rollups(
    seeded_db_with_failure: Path,
) -> None:
    """Groups are filtered consistently with totals — no skew."""
    # Default: openai group is the 3 success rows ($3 + $1.5 + $1.5 = $6).
    default_groups = {g.key: g for g in _query(group_by="provider").groups}
    assert default_groups["openai"].cost_usd == pytest.approx(6.0)
    assert default_groups["openai"].calls == 3

    # include_failed: openai group folds the failure row in.
    inclusive_groups = {g.key: g for g in _query(group_by="provider", include_failed=True).groups}
    assert inclusive_groups["openai"].cost_usd == pytest.approx(8.0)  # +$2
    assert inclusive_groups["openai"].calls == 4  # +1


def test_include_failed_propagates_to_tag_groups(seeded_db_with_failure: Path) -> None:
    """The CTE in `_tag_groups` honors include_failed too — symmetric paths."""
    # Seed an extra tagged failure row so it shows up in tag-grouping.
    with get_session() as session:
        record_event(
            session,
            provider="openai",
            model="gpt-test",
            input_tokens=1_000_000,
            output_tokens=0,
            tags=["prod"],
            success=False,
            error_type="connection_dropped",
            timestamp=_T0_MS - 4 * 3600 * 1000,
        )
        session.commit()

    default_tags = {g.key: g.calls for g in _query(group_by="tag").groups}
    inclusive_tags = {g.key: g.calls for g in _query(group_by="tag", include_failed=True).groups}
    # Baseline `prod` tag: 2 success rows. include_failed adds 1.
    assert default_tags["prod"] == 2
    assert inclusive_tags["prod"] == 3
