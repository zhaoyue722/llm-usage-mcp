"""Unit tests for `core.recommend.recommend`.

The MCP tool's behavior is covered by `test_recommend_provider.py` —
those tests run through the `@server.tool()` async wrapper. These
tests exercise the core function directly so the surface under test
is just the SQLAlchemy session + the parametric flag-name reference,
without the asyncio boilerplate. They also pin the contract for the
`tokens_flag_names` parameter that the CLI relies on.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from llm_usage.bootstrap import migrate_to_head
from llm_usage.core.db.session import get_session
from llm_usage.core.pricing import Pricing, upsert_pricing
from llm_usage.core.recommend import (
    NOMINAL_INPUT_TOKENS,
    NOMINAL_OUTPUT_TOKENS,
    recommend,
)

# Controlled rates so cost projections are exact dollars, not approx.
_PRICINGS = [
    Pricing("deepseek", "cheap-1", 1.0, 2.0, fetched_at=1),
    Pricing("openai", "mid-1", 2.0, 4.0, fetched_at=1),
    Pricing("anthropic", "premium-1", 5.0, 10.0, fetched_at=1),
]


@pytest.fixture
def priced_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db = tmp_path / "usage.db"
    monkeypatch.setenv("LLM_USAGE_DB_URL", f"sqlite:///{db}")
    migrate_to_head()
    with get_session() as session:
        upsert_pricing(session, _PRICINGS)
        session.commit()
    return db


@pytest.fixture
def empty_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db = tmp_path / "usage.db"
    monkeypatch.setenv("LLM_USAGE_DB_URL", f"sqlite:///{db}")
    migrate_to_head()
    return db


# --- core behavior -------------------------------------------------------


def test_recommend_picks_cheapest_at_1m_workload(priced_db: Path) -> None:
    """cheap-1 at $1/M in + $2/M out = $3.00 for a 1M/1M workload — beats mid-1 ($6) and premium-1 ($15)."""
    with get_session() as session:
        r = recommend(
            session,
            task_description="anything",
            expected_input_tokens=1_000_000,
            expected_output_tokens=1_000_000,
        )
    assert (r.provider, r.model) == ("deepseek", "cheap-1")
    assert r.estimated_cost_usd == pytest.approx(3.0)


def test_recommend_raises_when_pricing_empty(empty_db: Path) -> None:
    with get_session() as session, pytest.raises(ValueError, match="no priced models"):
        recommend(session, task_description="anything")


# --- nominal defaults ----------------------------------------------------


def test_recommend_defaults_to_nominal_workload(priced_db: Path) -> None:
    """Without `expected_*`, the workload is `NOMINAL_INPUT_TOKENS` / `NOMINAL_OUTPUT_TOKENS`."""
    assert NOMINAL_INPUT_TOKENS == 1_000
    assert NOMINAL_OUTPUT_TOKENS == 1_000
    with get_session() as session:
        r = recommend(session, task_description="anything")
    # 1k * $1/M + 1k * $2/M = $0.003 for cheap-1
    assert r.estimated_cost_usd == pytest.approx(0.003)
    assert "nominal defaults" in r.reasoning


def test_recommend_only_one_token_arg_still_triggers_default_note(priced_db: Path) -> None:
    """Either token arg being `None` should flag the result as nominal —
    the user can't partially specify a workload and get a 'precise' label."""
    with get_session() as session:
        r = recommend(
            session,
            task_description="anything",
            expected_input_tokens=8_000,
            expected_output_tokens=None,
        )
    assert "nominal defaults" in r.reasoning


def test_recommend_both_token_args_omits_default_note(priced_db: Path) -> None:
    with get_session() as session:
        r = recommend(
            session,
            task_description="anything",
            expected_input_tokens=8_000,
            expected_output_tokens=2_000,
        )
    assert "nominal defaults" not in r.reasoning


# --- budget --------------------------------------------------------------


def test_recommend_budget_filters_to_affordable(priced_db: Path) -> None:
    """Budget=$5 excludes premium-1 ($15), keeps cheap-1 + mid-1; still picks cheap-1."""
    with get_session() as session:
        r = recommend(
            session,
            task_description="anything",
            expected_input_tokens=1_000_000,
            expected_output_tokens=1_000_000,
            budget_usd=5.0,
        )
    assert (r.provider, r.model) == ("deepseek", "cheap-1")
    assert "$5.0000 budget" in r.reasoning


def test_recommend_over_budget_falls_back_to_cheapest_overall(priced_db: Path) -> None:
    """Budget = $0.01 fits nothing at 1M/1M. Falls back to cheap-1 with an explanation."""
    with get_session() as session:
        r = recommend(
            session,
            task_description="anything",
            expected_input_tokens=1_000_000,
            expected_output_tokens=1_000_000,
            budget_usd=0.01,
        )
    assert (r.provider, r.model) == ("deepseek", "cheap-1")
    assert "no priced model fits" in r.reasoning
    assert "$0.0100 budget" in r.reasoning


# --- reasoning content ---------------------------------------------------


def test_recommend_reasoning_echoes_task_description(priced_db: Path) -> None:
    with get_session() as session:
        r = recommend(session, task_description="summarize legal docs at scale")
    assert "summarize legal docs at scale" in r.reasoning


def test_recommend_reasoning_flags_v1_cost_only_semantics(priced_db: Path) -> None:
    """The user's most likely follow-up question is "why did you pick this?".
    The reasoning must surface that task_description didn't drive selection."""
    with get_session() as session:
        r = recommend(session, task_description="anything")
    assert "v1 ranks by cost only" in r.reasoning
    assert "does not drive selection" in r.reasoning


# --- tokens_flag_names parameter ----------------------------------------


def test_recommend_default_flag_names_match_mcp_param_names(priced_db: Path) -> None:
    """The default `tokens_flag_names` should produce reasoning that
    matches the MCP tool's parameter surface — back-compat with the
    `test_recommend_provider.py` assertions on the same phrase."""
    with get_session() as session:
        r = recommend(session, task_description="anything")
    assert "expected_input_tokens" in r.reasoning
    assert "expected_output_tokens" in r.reasoning


def test_recommend_custom_flag_names_surface_in_reasoning(priced_db: Path) -> None:
    """The CLI passes `("--in", "--out")` so its users see CLI flag
    names in the nominal-defaults hint, not Python parameter names."""
    with get_session() as session:
        r = recommend(
            session,
            task_description="anything",
            tokens_flag_names=("--in", "--out"),
        )
    assert "--in" in r.reasoning
    assert "--out" in r.reasoning
    assert "expected_input_tokens" not in r.reasoning


def test_recommend_custom_flag_names_only_appear_when_defaults_triggered(
    priced_db: Path,
) -> None:
    """The flag-name advice is part of the 'nominal defaults' hint —
    if the caller supplies both token counts, no advice phrase is
    emitted (regardless of which flag names were passed)."""
    with get_session() as session:
        r = recommend(
            session,
            task_description="anything",
            expected_input_tokens=1_000,
            expected_output_tokens=1_000,
            tokens_flag_names=("--in", "--out"),
        )
    assert "--in" not in r.reasoning
    assert "--out" not in r.reasoning
