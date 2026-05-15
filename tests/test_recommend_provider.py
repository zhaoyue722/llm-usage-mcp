"""Tests for the `recommend_provider` MCP tool.

A small controlled fixture: three scored models (cheap-low-quality,
mid, premium) plus one priced-but-unscored model that must be excluded
from candidates. Cost math is exact (round-number rates and round-number
workloads), so per-priority picks and the budget-fallback case can be
asserted by name, not by `pytest.approx` of vendored values.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import llm_usage.mcp.server as server_module
from llm_usage.bootstrap import migrate_to_head
from llm_usage.core.db.session import get_session
from llm_usage.core.models import QualityPriority, RecommendProviderResult
from llm_usage.core.pricing import Pricing, upsert_pricing
from llm_usage.core.quality import Quality, upsert_quality

# All four pricing rows; only the first three get quality scores.
#   cheap-1 : $1/M in, $2/M out, quality 70 — cheap-low
#   mid-1   : $2/M in, $4/M out, quality 85 — mid
#   premium-1: $5/M in, $10/M out, quality 95 — flagship
#   unscored-1: $0.50/M in, $1/M out — has pricing but NO quality entry
_PRICINGS = [
    Pricing("deepseek", "cheap-1", 1.0, 2.0, fetched_at=1),
    Pricing("openai", "mid-1", 2.0, 4.0, fetched_at=1),
    Pricing("anthropic", "premium-1", 5.0, 10.0, fetched_at=1),
    Pricing("qwen", "unscored-1", 0.5, 1.0, fetched_at=1),
]

_QUALITIES = [
    Quality("deepseek", "cheap-1", 70.0, fetched_at=1),
    Quality("openai", "mid-1", 85.0, fetched_at=1),
    Quality("anthropic", "premium-1", 95.0, fetched_at=1),
]

# 1M / 1M workload projected costs given the rates above:
#   cheap-1   : 1M * $1/M + 1M * $2/M = $3.00
#   mid-1     : 1M * $2/M + 1M * $4/M = $6.00
#   premium-1 : 1M * $5/M + 1M * $10/M = $15.00


@pytest.fixture
def scored_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Fresh DB with the schema, controlled pricing + quality rows."""
    db = tmp_path / "usage.db"
    monkeypatch.setenv("LLM_USAGE_DB_URL", f"sqlite:///{db}")
    migrate_to_head()
    with get_session() as session:
        upsert_pricing(session, _PRICINGS)
        upsert_quality(session, _QUALITIES)
        session.commit()
    return db


@pytest.fixture
def empty_quality_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Fresh DB with pricing but no quality scores."""
    db = tmp_path / "usage.db"
    monkeypatch.setenv("LLM_USAGE_DB_URL", f"sqlite:///{db}")
    migrate_to_head()
    with get_session() as session:
        upsert_pricing(session, _PRICINGS)
        session.commit()
    return db


def _recommend(
    *,
    task_description: str = "a representative task",
    expected_input_tokens: int | None = 1_000_000,
    expected_output_tokens: int | None = 1_000_000,
    budget_usd: float | None = None,
    quality_priority: QualityPriority | None = None,
) -> RecommendProviderResult:
    # `@server.tool()` erases the return type for mypy; pin it back.
    result: RecommendProviderResult = asyncio.run(
        server_module.recommend_provider(
            task_description=task_description,
            expected_input_tokens=expected_input_tokens,
            expected_output_tokens=expected_output_tokens,
            budget_usd=budget_usd,
            quality_priority=quality_priority,
        )
    )
    return result


# --- per-priority picks ----------------------------------------------------


def test_lowest_cost_picks_cheapest_scored_model(scored_db: Path) -> None:
    r = _recommend(quality_priority="lowest_cost")
    assert (r.provider, r.model) == ("deepseek", "cheap-1")
    assert r.estimated_cost_usd == pytest.approx(3.0)


def test_highest_quality_picks_top_score(scored_db: Path) -> None:
    r = _recommend(quality_priority="highest_quality")
    assert (r.provider, r.model) == ("anthropic", "premium-1")
    assert r.estimated_cost_usd == pytest.approx(15.0)


def test_balanced_picks_the_middle_tier(scored_db: Path) -> None:
    """50/50 min-max blend: cheap is too low-quality, premium is too expensive."""
    r = _recommend(quality_priority="balanced")
    assert (r.provider, r.model) == ("openai", "mid-1")
    assert r.estimated_cost_usd == pytest.approx(6.0)


def test_default_priority_is_balanced(scored_db: Path) -> None:
    """No priority passed -> balanced semantics."""
    r = _recommend(quality_priority=None)
    assert (r.provider, r.model) == ("openai", "mid-1")


# --- candidate set ---------------------------------------------------------


def test_priced_but_unscored_models_are_excluded(scored_db: Path) -> None:
    """`unscored-1` is the cheapest priced model but has no quality row."""
    r = _recommend(quality_priority="lowest_cost")
    # Despite being cheaper ($1.50), unscored-1 isn't a candidate.
    assert r.model != "unscored-1"
    assert (r.provider, r.model) == ("deepseek", "cheap-1")


def test_empty_quality_table_raises(empty_quality_db: Path) -> None:
    with pytest.raises(ValueError, match="no scored models"):
        _recommend(quality_priority="lowest_cost")


# --- budget filter ---------------------------------------------------------


def test_budget_filters_to_affordable(scored_db: Path) -> None:
    """$10 budget excludes premium-1 ($15); highest_quality picks mid-1."""
    r = _recommend(quality_priority="highest_quality", budget_usd=10.0)
    assert (r.provider, r.model) == ("openai", "mid-1")
    assert "$10.0000 budget" in r.reasoning


def test_over_budget_falls_back_to_cheapest_with_explanation(
    scored_db: Path,
) -> None:
    """Budget below every model: pick the cheapest, regardless of priority."""
    r = _recommend(quality_priority="highest_quality", budget_usd=0.01)
    assert (r.provider, r.model) == ("deepseek", "cheap-1")
    assert "no scored model fits" in r.reasoning
    assert "$0.0100 budget" in r.reasoning


def test_budget_exactly_at_cheapest_is_inclusive(scored_db: Path) -> None:
    """A budget at the cheapest cost includes that model (<= comparison)."""
    r = _recommend(quality_priority="lowest_cost", budget_usd=3.0)
    assert (r.provider, r.model) == ("deepseek", "cheap-1")


# --- reasoning -------------------------------------------------------------


def test_reasoning_echoes_task_description(scored_db: Path) -> None:
    r = _recommend(task_description="summarize legal docs", quality_priority="balanced")
    assert "summarize legal docs" in r.reasoning


def test_reasoning_notes_when_tokens_defaulted(scored_db: Path) -> None:
    r = _recommend(
        expected_input_tokens=None,
        expected_output_tokens=None,
        quality_priority="lowest_cost",
    )
    assert "nominal defaults" in r.reasoning
    # 1k/1k nominal at $1+$2 per M = $0.003 for cheap-1.
    assert r.estimated_cost_usd == pytest.approx(0.003)


def test_reasoning_no_default_note_when_tokens_provided(scored_db: Path) -> None:
    r = _recommend(quality_priority="lowest_cost")
    assert "nominal defaults" not in r.reasoning


def test_reasoning_mentions_chosen_provider_and_model(scored_db: Path) -> None:
    r = _recommend(quality_priority="balanced")
    assert "openai/mid-1" in r.reasoning


def test_reasoning_mentions_priority_label(scored_db: Path) -> None:
    r = _recommend(quality_priority="highest_quality")
    assert "highest_quality" in r.reasoning


# --- edge cases ------------------------------------------------------------


def test_zero_token_workload_balanced_falls_back_to_highest_quality(
    scored_db: Path,
) -> None:
    """With all costs == 0, the balanced blend ranks purely by quality."""
    r = _recommend(
        expected_input_tokens=0,
        expected_output_tokens=0,
        quality_priority="balanced",
    )
    # All costs are 0 -> cost dimension cancels out -> quality wins.
    assert (r.provider, r.model) == ("anthropic", "premium-1")
    assert r.estimated_cost_usd == 0.0


def test_single_scored_model_is_always_picked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One candidate -> returned regardless of priority (no normalization issues)."""
    db = tmp_path / "usage.db"
    monkeypatch.setenv("LLM_USAGE_DB_URL", f"sqlite:///{db}")
    migrate_to_head()
    with get_session() as session:
        upsert_pricing(session, [_PRICINGS[1]])  # only mid-1
        upsert_quality(session, [_QUALITIES[1]])
        session.commit()

    priorities: list[QualityPriority] = ["lowest_cost", "balanced", "highest_quality"]
    for priority in priorities:
        r = _recommend(quality_priority=priority)
        assert (r.provider, r.model) == ("openai", "mid-1")
