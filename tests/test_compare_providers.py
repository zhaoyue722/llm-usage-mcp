"""Tests for the `compare_providers` MCP tool.

Cost projection is asserted exactly against controlled `Pricing` rows
inserted by the test (round numbers), not `pytest.approx` of vendored
LiteLLM values — so the tests don't drift on a pricing refresh.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import llm_usage.mcp.server as server_module
from llm_usage.bootstrap import migrate_to_head
from llm_usage.core.db.session import get_session
from llm_usage.core.models import CompareProvidersResult, TaskType
from llm_usage.core.pricing import Pricing, upsert_pricing

# Three controlled models with round per-million rates so both the
# absolute cost and the relative_cost_pct come out exact.
#   cheap  : $1/M in,  $2/M out
#   mid    : $2/M in,  $4/M out   (2x cheap)
#   premium: $5/M in, $10/M out   (5x cheap)
_PRICINGS = [
    Pricing(
        provider="deepseek",
        model="cheap-1",
        input_per_million_usd=1.0,
        output_per_million_usd=2.0,
        fetched_at=1_700_000_000_000,
    ),
    Pricing(
        provider="openai",
        model="mid-1",
        input_per_million_usd=2.0,
        output_per_million_usd=4.0,
        fetched_at=1_700_000_000_000,
    ),
    Pricing(
        provider="anthropic",
        model="premium-1",
        input_per_million_usd=5.0,
        output_per_million_usd=10.0,
        fetched_at=1_700_000_000_000,
    ),
]


@pytest.fixture
def priced_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Fresh DB with the schema and three controlled pricing rows."""
    db = tmp_path / "usage.db"
    monkeypatch.setenv("LLM_USAGE_DB_URL", f"sqlite:///{db}")
    migrate_to_head()
    with get_session() as session:
        upsert_pricing(session, _PRICINGS)
        session.commit()
    return db


@pytest.fixture
def empty_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Fresh DB with the schema but no pricing rows."""
    db = tmp_path / "usage.db"
    monkeypatch.setenv("LLM_USAGE_DB_URL", f"sqlite:///{db}")
    migrate_to_head()
    return db


def _compare(
    *,
    expected_input_tokens: int,
    expected_output_tokens: int,
    task_type: TaskType | None = None,
    models: list[str] | None = None,
    include_cached_estimate: bool = False,
) -> CompareProvidersResult:
    # `@server.tool()` erases the return type for mypy's view; the local
    # annotation pins it back to the real `*Result` model.
    result: CompareProvidersResult = asyncio.run(
        server_module.compare_providers(
            expected_input_tokens=expected_input_tokens,
            expected_output_tokens=expected_output_tokens,
            task_type=task_type,
            models=models,
            include_cached_estimate=include_cached_estimate,
        )
    )
    return result


# --- ranking + cost --------------------------------------------------------


def test_ranks_cheapest_first(priced_db: Path) -> None:
    result = _compare(expected_input_tokens=1_000_000, expected_output_tokens=1_000_000)
    models = [entry.model for entry in result.ranked]
    assert models == ["cheap-1", "mid-1", "premium-1"]


def test_absolute_cost_is_exact(priced_db: Path) -> None:
    result = _compare(expected_input_tokens=1_000_000, expected_output_tokens=1_000_000)
    by_model = {entry.model: entry for entry in result.ranked}
    # cheap: 1M @ $1/M + 1M @ $2/M = $3.00
    assert by_model["cheap-1"].cost_usd == pytest.approx(3.0)
    # mid: 1M @ $2/M + 1M @ $4/M = $6.00
    assert by_model["mid-1"].cost_usd == pytest.approx(6.0)
    # premium: 1M @ $5/M + 1M @ $10/M = $15.00
    assert by_model["premium-1"].cost_usd == pytest.approx(15.0)


def test_relative_cost_pct_against_cheapest(priced_db: Path) -> None:
    result = _compare(expected_input_tokens=1_000_000, expected_output_tokens=1_000_000)
    by_model = {entry.model: entry for entry in result.ranked}
    # cheapest is the 100% baseline; mid is 2x, premium is 5x.
    assert by_model["cheap-1"].relative_cost_pct == 100.0
    assert by_model["mid-1"].relative_cost_pct == 200.0
    assert by_model["premium-1"].relative_cost_pct == 500.0


def test_every_model_appears(priced_db: Path) -> None:
    result = _compare(expected_input_tokens=100, expected_output_tokens=100)
    assert len(result.ranked) == 3
    assert {e.provider for e in result.ranked} == {"deepseek", "openai", "anthropic"}


# --- models filter ---------------------------------------------------------


def test_models_filter_restricts(priced_db: Path) -> None:
    result = _compare(
        expected_input_tokens=1_000_000,
        expected_output_tokens=0,
        models=["cheap-1", "premium-1"],
    )
    models = [entry.model for entry in result.ranked]
    assert models == ["cheap-1", "premium-1"]  # mid-1 excluded
    # relative_cost_pct re-baselines against the cheapest *in the filtered set*.
    by_model = {entry.model: entry for entry in result.ranked}
    assert by_model["cheap-1"].relative_cost_pct == 100.0
    assert by_model["premium-1"].relative_cost_pct == 500.0


def test_models_filter_matching_nothing_returns_empty(priced_db: Path) -> None:
    result = _compare(
        expected_input_tokens=100,
        expected_output_tokens=100,
        models=["does-not-exist"],
    )
    assert result.ranked == []


# --- empty / edge cases ----------------------------------------------------


def test_empty_pricing_table_returns_empty_ranked(empty_db: Path) -> None:
    result = _compare(expected_input_tokens=100, expected_output_tokens=100)
    assert result.ranked == []


def test_zero_token_workload_all_cost_zero_all_100_pct(priced_db: Path) -> None:
    result = _compare(expected_input_tokens=0, expected_output_tokens=0)
    assert len(result.ranked) == 3
    for entry in result.ranked:
        assert entry.cost_usd == 0.0
        assert entry.relative_cost_pct == 100.0


# --- include_cached_estimate / task_type -----------------------------------


def test_cached_estimate_flag_sets_note(priced_db: Path) -> None:
    result = _compare(
        expected_input_tokens=1_000_000,
        expected_output_tokens=0,
        include_cached_estimate=True,
    )
    assert result.ranked
    for entry in result.ranked:
        assert entry.notes is not None
        assert "not applied in v1" in entry.notes


def test_cached_estimate_flag_does_not_change_cost(priced_db: Path) -> None:
    """The flag is accepted but must not alter the projected cost."""
    without = _compare(expected_input_tokens=1_000_000, expected_output_tokens=500_000)
    with_flag = _compare(
        expected_input_tokens=1_000_000,
        expected_output_tokens=500_000,
        include_cached_estimate=True,
    )
    assert [e.cost_usd for e in without.ranked] == [e.cost_usd for e in with_flag.ranked]


def test_no_cached_estimate_leaves_notes_none(priced_db: Path) -> None:
    result = _compare(expected_input_tokens=100, expected_output_tokens=100)
    assert result.ranked
    assert all(entry.notes is None for entry in result.ranked)


def test_task_type_accepted_and_does_not_change_ranking(priced_db: Path) -> None:
    """task_type is accepted but task-independent — ranking is unchanged."""
    plain = _compare(expected_input_tokens=1_000, expected_output_tokens=1_000)
    with_type = _compare(
        expected_input_tokens=1_000,
        expected_output_tokens=1_000,
        task_type="code",
    )
    assert [e.model for e in plain.ranked] == [e.model for e in with_type.ranked]
    assert [e.cost_usd for e in plain.ranked] == [e.cost_usd for e in with_type.ranked]
