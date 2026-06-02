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


# --- providers / models filter ------------------------------------------


def test_recommend_providers_filter_restricts_to_named_providers(
    priced_db: Path,
) -> None:
    """`providers=["openai"]` excludes cheap-1 (deepseek) and premium-1
    (anthropic), leaving only mid-1 (openai) — so mid-1 wins despite
    not being the cheapest overall."""
    with get_session() as session:
        r = recommend(
            session,
            task_description="anything",
            expected_input_tokens=1_000_000,
            expected_output_tokens=1_000_000,
            providers=["openai"],
        )
    assert (r.provider, r.model) == ("openai", "mid-1")


def test_recommend_models_filter_restricts_to_named_models(priced_db: Path) -> None:
    """`models=["mid-1", "premium-1"]` excludes cheap-1, leaving mid-1
    and premium-1 — so mid-1 wins as the cheaper of the two."""
    with get_session() as session:
        r = recommend(
            session,
            task_description="anything",
            expected_input_tokens=1_000_000,
            expected_output_tokens=1_000_000,
            models=["mid-1", "premium-1"],
        )
    assert (r.provider, r.model) == ("openai", "mid-1")


def test_recommend_provider_and_model_filters_and_combine(priced_db: Path) -> None:
    """Both filters together: only rows in both whitelists qualify.
    `providers=["openai"]` + `models=["mid-1", "cheap-1"]` keeps only
    openai/mid-1 (cheap-1 is deepseek, filtered by providers)."""
    with get_session() as session:
        r = recommend(
            session,
            task_description="anything",
            expected_input_tokens=1_000_000,
            expected_output_tokens=1_000_000,
            providers=["openai"],
            models=["mid-1", "cheap-1"],
        )
    assert (r.provider, r.model) == ("openai", "mid-1")


def test_recommend_filters_apply_before_budget_check(priced_db: Path) -> None:
    """A budget that's over premium-1's $15 but under any cheaper model
    — combined with `providers=["anthropic"]` — should hit the
    over-budget fallback within anthropic, not silently fall back to
    cheap-1 (which is filtered out)."""
    with get_session() as session:
        r = recommend(
            session,
            task_description="anything",
            expected_input_tokens=1_000_000,
            expected_output_tokens=1_000_000,
            providers=["anthropic"],
            budget_usd=10.0,
        )
    # anthropic/premium-1 is over-budget ($15 > $10) but it's the only
    # candidate after the provider filter — fallback to it.
    assert (r.provider, r.model) == ("anthropic", "premium-1")
    assert "no priced model fits" in r.reasoning


def test_recommend_empty_filter_match_raises_specific_error(
    priced_db: Path,
) -> None:
    """A whitelist that names something not in the catalog raises a
    distinct error mentioning the filter — so a typo doesn't manifest
    as the misleading 'database not bootstrapped' message."""
    with (
        get_session() as session,
        pytest.raises(ValueError, match="no priced models match the recommend filter"),
    ):
        recommend(
            session,
            task_description="anything",
            providers=["does-not-exist"],
        )


def test_recommend_empty_provider_filter_error_lists_filter_contents(
    priced_db: Path,
) -> None:
    """The no-match error should echo the failing filter's contents so
    the user can spot a typo at a glance."""
    with get_session() as session, pytest.raises(ValueError) as excinfo:
        recommend(
            session,
            task_description="anything",
            providers=["typoed-name"],
            models=["also-typoed"],
        )
    msg = str(excinfo.value)
    assert "typoed-name" in msg
    assert "also-typoed" in msg


def test_recommend_unknown_names_dont_raise_when_other_matches_exist(
    priced_db: Path,
) -> None:
    """Mixed lists with one real and one bogus name should still pick
    the real one — symmetric with `get_pricing`'s 'unknown returns
    empty' behavior. The filter narrows to {bogus, real} ∩ catalog =
    {real}, which is non-empty."""
    with get_session() as session:
        r = recommend(
            session,
            task_description="anything",
            expected_input_tokens=1_000_000,
            expected_output_tokens=1_000_000,
            models=["cheap-1", "does-not-exist"],
        )
    assert (r.provider, r.model) == ("deepseek", "cheap-1")


def test_recommend_none_filters_behave_like_no_filter(priced_db: Path) -> None:
    """`providers=None` and `models=None` (the defaults) should leave
    the candidate pool untouched — backward compatible with the pre-
    filter behavior."""
    with get_session() as session:
        unfiltered = recommend(
            session,
            task_description="anything",
            expected_input_tokens=1_000_000,
            expected_output_tokens=1_000_000,
        )
        nones = recommend(
            session,
            task_description="anything",
            expected_input_tokens=1_000_000,
            expected_output_tokens=1_000_000,
            providers=None,
            models=None,
        )
    assert (unfiltered.provider, unfiltered.model) == (nones.provider, nones.model)
    assert unfiltered.estimated_cost_usd == nones.estimated_cost_usd
