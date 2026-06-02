"""Tests for the `recommend_provider` MCP tool.

v1 ranks by projected cost only — `quality_priority` and the
quality-table join were removed before shipping because the only
available quality data was hand-authored editorial scores (see
`docs/re_evaluation_2026_05_15.md`). A small controlled fixture with
round-number rates lets per-call picks be asserted by name and exact
dollars, no `pytest.approx` of vendored values.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import llm_usage.mcp.server as server_module
from llm_usage.bootstrap import migrate_to_head
from llm_usage.core.db.session import get_session
from llm_usage.core.models import RecommendProviderResult
from llm_usage.core.pricing import Pricing, upsert_pricing

# Three controlled models with round rates so cost projections are exact.
#   cheap-1   : $1/M in,  $2/M out
#   mid-1     : $2/M in,  $4/M out   (2x cheap)
#   premium-1 : $5/M in, $10/M out   (5x cheap)
_PRICINGS = [
    Pricing("deepseek", "cheap-1", 1.0, 2.0, fetched_at=1),
    Pricing("openai", "mid-1", 2.0, 4.0, fetched_at=1),
    Pricing("anthropic", "premium-1", 5.0, 10.0, fetched_at=1),
]

# 1M / 1M workload projected costs given the rates above:
#   cheap-1   : 1M * $1/M + 1M * $2/M = $3.00
#   mid-1     : 1M * $2/M + 1M * $4/M = $6.00
#   premium-1 : 1M * $5/M + 1M * $10/M = $15.00


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


def _recommend(
    *,
    task_description: str = "a representative task",
    expected_input_tokens: int | None = 1_000_000,
    expected_output_tokens: int | None = 1_000_000,
    budget_usd: float | None = None,
    providers: list[str] | None = None,
    models: list[str] | None = None,
) -> RecommendProviderResult:
    # `@server.tool()` erases the return type for mypy; pin it back.
    result: RecommendProviderResult = asyncio.run(
        server_module.recommend_provider(
            task_description=task_description,
            expected_input_tokens=expected_input_tokens,
            expected_output_tokens=expected_output_tokens,
            budget_usd=budget_usd,
            providers=providers,
            models=models,
        )
    )
    return result


# --- selection -------------------------------------------------------------


def test_picks_cheapest_for_workload(priced_db: Path) -> None:
    r = _recommend()
    assert (r.provider, r.model) == ("deepseek", "cheap-1")
    assert r.estimated_cost_usd == pytest.approx(3.0)


def test_empty_pricing_table_raises(empty_db: Path) -> None:
    with pytest.raises(ValueError, match="no priced models"):
        _recommend()


# --- budget filter ---------------------------------------------------------


def test_budget_filters_to_affordable(priced_db: Path) -> None:
    """$10 budget excludes premium-1 ($15); the cheapest fitting wins."""
    r = _recommend(budget_usd=10.0)
    assert (r.provider, r.model) == ("deepseek", "cheap-1")
    assert "$10.0000 budget" in r.reasoning


def test_budget_exactly_at_cheapest_is_inclusive(priced_db: Path) -> None:
    """A budget at the cheapest cost includes that model (<= comparison)."""
    r = _recommend(budget_usd=3.0)
    assert (r.provider, r.model) == ("deepseek", "cheap-1")


def test_over_budget_falls_back_to_cheapest_with_explanation(priced_db: Path) -> None:
    """Budget below every model: pick the cheapest overall."""
    r = _recommend(budget_usd=0.01)
    assert (r.provider, r.model) == ("deepseek", "cheap-1")
    assert "no priced model fits" in r.reasoning
    assert "$0.0100 budget" in r.reasoning


# --- reasoning -------------------------------------------------------------


def test_reasoning_echoes_task_description(priced_db: Path) -> None:
    r = _recommend(task_description="summarize legal docs")
    assert "summarize legal docs" in r.reasoning


def test_reasoning_flags_v1_cost_only_semantics(priced_db: Path) -> None:
    """The reasoning must surface that task_description doesn't drive selection."""
    r = _recommend()
    assert "v1 ranks by cost only" in r.reasoning
    assert "does not drive selection" in r.reasoning


def test_reasoning_notes_when_tokens_defaulted(priced_db: Path) -> None:
    r = _recommend(expected_input_tokens=None, expected_output_tokens=None)
    assert "nominal defaults" in r.reasoning
    # 1k/1k nominal at $1+$2 per M = $0.003 for cheap-1.
    assert r.estimated_cost_usd == pytest.approx(0.003)


def test_reasoning_no_default_note_when_tokens_provided(priced_db: Path) -> None:
    r = _recommend()
    assert "nominal defaults" not in r.reasoning


def test_reasoning_mentions_chosen_provider_and_model(priced_db: Path) -> None:
    r = _recommend()
    assert "deepseek/cheap-1" in r.reasoning


# --- edge cases ------------------------------------------------------------


def test_zero_token_workload_picks_alphabetically_first(priced_db: Path) -> None:
    """All projected costs are $0 — stable sort keeps (provider, model) order."""
    r = _recommend(expected_input_tokens=0, expected_output_tokens=0)
    # `all_pricing` returns (provider, model)-sorted: anthropic/premium-1 first.
    assert (r.provider, r.model) == ("anthropic", "premium-1")
    assert r.estimated_cost_usd == 0.0


def test_single_priced_model_is_always_picked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One candidate -> returned regardless of budget shape."""
    db = tmp_path / "usage.db"
    monkeypatch.setenv("LLM_USAGE_DB_URL", f"sqlite:///{db}")
    migrate_to_head()
    with get_session() as session:
        upsert_pricing(session, [_PRICINGS[1]])  # only mid-1
        session.commit()

    r = _recommend()
    assert (r.provider, r.model) == ("openai", "mid-1")
    # Same answer when over-budget falls back to cheapest.
    r2 = _recommend(budget_usd=0.0001)
    assert (r2.provider, r2.model) == ("openai", "mid-1")


# --- providers / models filters (MCP surface) ---------------------------


def test_providers_filter_passes_through_mcp_tool(priced_db: Path) -> None:
    """The new `providers` param should reach `core/recommend.recommend`
    and reshape the winner — pins the MCP-tool plumbing."""
    r = _recommend(providers=["openai"])
    assert (r.provider, r.model) == ("openai", "mid-1")


def test_models_filter_passes_through_mcp_tool(priced_db: Path) -> None:
    """Symmetric pin for the `models` param on the MCP tool."""
    r = _recommend(models=["mid-1", "premium-1"])
    assert (r.provider, r.model) == ("openai", "mid-1")


def test_filter_with_no_match_raises_via_mcp(priced_db: Path) -> None:
    """The no-match error should bubble through the async tool wrapper
    as a plain `ValueError` — MCP clients get a structured error
    rather than a fabricated result."""
    with pytest.raises(ValueError, match="no priced models match"):
        _recommend(providers=["typo"])
