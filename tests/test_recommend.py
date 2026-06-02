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


def test_recommend_provider_filter_is_case_insensitive(priced_db: Path) -> None:
    """Provider names have a well-known canonical case (lowercase in
    DB, branded in display). A user typing the branded form they
    see in output (`OpenAI`) should match the lowercase DB row
    (`openai`) — case sensitivity here is a UX trap, not a feature.

    Pins the branded-form input behavior for the four v1 providers,
    plus the lowercase form to confirm both still work.
    """
    branded_inputs = ["Anthropic", "OpenAI", "DeepSeek", "openai"]
    for name in branded_inputs:
        with get_session() as session:
            r = recommend(
                session,
                task_description="anything",
                expected_input_tokens=1_000_000,
                expected_output_tokens=1_000_000,
                providers=[name],
            )
        # Every branded form should resolve to a real model from that
        # provider — exact pick depends on the fixture's cheapest
        # within each provider, but the provider lowercase should
        # always be the right one.
        assert r.provider == name.lower(), f"case-insensitive lookup failed for {name!r}"


def test_recommend_model_filter_stays_case_sensitive(priced_db: Path) -> None:
    """Model names are open catalog literals. Case-folding them risks
    colliding two distinct entries that differ only by case (unlikely
    with LiteLLM today, but the safer default). Pins the strict-match
    behavior so it doesn't silently drift to case-insensitive.
    """
    with (
        get_session() as session,
        pytest.raises(ValueError, match="no priced models match the recommend filter"),
    ):
        recommend(
            session,
            task_description="anything",
            models=["CHEAP-1"],  # the fixture row is `cheap-1` lowercase
        )


# --- alternatives -------------------------------------------------------


def test_recommend_alternatives_carry_top_two_runner_ups(priced_db: Path) -> None:
    """With three priced models in the fixture, the chosen row is
    cheap-1 and the alternatives are mid-1, premium-1 (cost-asc)."""
    with get_session() as session:
        r = recommend(
            session,
            task_description="anything",
            expected_input_tokens=1_000_000,
            expected_output_tokens=1_000_000,
        )
    assert (r.provider, r.model) == ("deepseek", "cheap-1")
    assert [(a.provider, a.model) for a in r.alternatives] == [
        ("openai", "mid-1"),
        ("anthropic", "premium-1"),
    ]
    # Costs scale with the rates: mid-1 $6, premium-1 $15.
    assert r.alternatives[0].estimated_cost_usd == pytest.approx(6.0)
    assert r.alternatives[1].estimated_cost_usd == pytest.approx(15.0)


def test_recommend_alternatives_exclude_the_chosen_row(priced_db: Path) -> None:
    """The chosen row must not also appear in alternatives — every
    entry in `alternatives` is strictly distinct from `(provider,
    model)` on the result."""
    with get_session() as session:
        r = recommend(
            session,
            task_description="anything",
            expected_input_tokens=1_000_000,
            expected_output_tokens=1_000_000,
        )
    chosen = (r.provider, r.model)
    for alt in r.alternatives:
        assert (alt.provider, alt.model) != chosen


def test_recommend_alternatives_empty_when_pool_has_one_model(
    priced_db: Path,
) -> None:
    """A filter that narrows the candidate pool to exactly one row
    yields an empty alternatives list. The renderer uses this as a
    signal to skip the section."""
    with get_session() as session:
        r = recommend(
            session,
            task_description="anything",
            expected_input_tokens=1_000_000,
            expected_output_tokens=1_000_000,
            models=["cheap-1"],
        )
    assert (r.provider, r.model) == ("deepseek", "cheap-1")
    assert r.alternatives == []


def test_recommend_alternatives_size_when_pool_has_two_models(
    priced_db: Path,
) -> None:
    """Pool=2 → 1 alternative. Confirms the slice doesn't IndexError
    when the pool is smaller than the default count."""
    with get_session() as session:
        r = recommend(
            session,
            task_description="anything",
            expected_input_tokens=1_000_000,
            expected_output_tokens=1_000_000,
            models=["cheap-1", "mid-1"],
        )
    assert (r.provider, r.model) == ("deepseek", "cheap-1")
    assert len(r.alternatives) == 1
    assert (r.alternatives[0].provider, r.alternatives[0].model) == ("openai", "mid-1")


def test_recommend_alternatives_respect_provider_filter(priced_db: Path) -> None:
    """When `--provider openai` is set, alternatives must also be
    from openai — otherwise they'd contradict the user's filter."""
    with get_session() as session:
        r = recommend(
            session,
            task_description="anything",
            expected_input_tokens=1_000_000,
            expected_output_tokens=1_000_000,
            providers=["openai"],
        )
    # Only one openai model in the fixture (mid-1), so no alternatives.
    assert (r.provider, r.model) == ("openai", "mid-1")
    assert r.alternatives == []
    # All alternatives that DO exist (in a multi-row provider) must
    # have the right provider.
    for alt in r.alternatives:
        assert alt.provider == "openai"


def test_recommend_alternatives_drawn_from_affordable_pool(priced_db: Path) -> None:
    """When the budget filter is active, alternatives come from the
    affordable pool — not the full set. budget=$5 keeps cheap-1
    ($3) and excludes mid-1 ($6) + premium-1 ($15), so alternatives
    is empty (only one model fits)."""
    with get_session() as session:
        r = recommend(
            session,
            task_description="anything",
            expected_input_tokens=1_000_000,
            expected_output_tokens=1_000_000,
            budget_usd=5.0,
        )
    assert (r.provider, r.model) == ("deepseek", "cheap-1")
    assert r.alternatives == []


def test_recommend_alternatives_drawn_from_full_pool_on_over_budget_fallback(
    priced_db: Path,
) -> None:
    """When the budget filter triggers the over-budget fallback,
    alternatives come from the filtered (not affordable) pool — same
    pool the chosen row was drawn from, per the spec."""
    with get_session() as session:
        r = recommend(
            session,
            task_description="anything",
            expected_input_tokens=1_000_000,
            expected_output_tokens=1_000_000,
            budget_usd=0.01,  # below cheapest at this workload
        )
    # Over budget: chosen is the cheapest of the FULL pool.
    assert (r.provider, r.model) == ("deepseek", "cheap-1")
    # Alternatives are the next two from the full pool, not "nothing
    # affordable so nothing to suggest."
    assert len(r.alternatives) == 2
    assert [(a.provider, a.model) for a in r.alternatives] == [
        ("openai", "mid-1"),
        ("anthropic", "premium-1"),
    ]


# --- family-dedup alternatives ------------------------------------------


@pytest.fixture
def family_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """DB with alias/snapshot family clusters for dedup tests."""
    db = tmp_path / "usage.db"
    monkeypatch.setenv("LLM_USAGE_DB_URL", f"sqlite:///{db}")
    migrate_to_head()
    with get_session() as session:
        upsert_pricing(
            session,
            [
                # qwen-foo family (cheapest), 4 catalog rows at identical price.
                Pricing("qwen", "qwen-foo", 1.0, 2.0, fetched_at=1),
                Pricing("qwen", "qwen-foo-2024-11-01", 1.0, 2.0, fetched_at=1),
                Pricing("qwen", "qwen-foo-2025-04-28", 1.0, 2.0, fetched_at=1),
                Pricing("qwen", "qwen-foo-latest", 1.0, 2.0, fetched_at=1),
                # Different family inside qwen — should NOT be skipped
                # by alternative family-dedup.
                Pricing("qwen", "qwen-bar", 2.0, 4.0, fetched_at=1),
                # OpenAI family: alias + snapshot at the same price.
                Pricing("openai", "gpt-mid", 3.0, 6.0, fetched_at=1),
                Pricing("openai", "gpt-mid-2025-08-07", 3.0, 6.0, fetched_at=1),
                # Anthropic, unique family.
                Pricing("anthropic", "claude-x", 5.0, 10.0, fetched_at=1),
            ],
        )
        session.commit()
    return db


def test_recommend_alternatives_skip_alias_variants_of_chosen(
    family_db: Path,
) -> None:
    """Chosen = qwen-foo (cheapest in family). The 3 sibling
    snapshots/alias variants must be skipped from alternatives —
    they're the same logical model."""
    with get_session() as session:
        r = recommend(
            session,
            task_description="anything",
            expected_input_tokens=1_000_000,
            expected_output_tokens=1_000_000,
        )
    assert r.model == "qwen-foo"
    alt_models = [a.model for a in r.alternatives]
    assert "qwen-foo-2024-11-01" not in alt_models
    assert "qwen-foo-2025-04-28" not in alt_models
    assert "qwen-foo-latest" not in alt_models


def test_recommend_alternatives_skip_alias_variants_of_prior_alternatives(
    family_db: Path,
) -> None:
    """When `gpt-mid` and `gpt-mid-2025-08-07` would otherwise both
    qualify as alternatives, only the alias appears — the snapshot is
    skipped because the family root is already represented."""
    with get_session() as session:
        r = recommend(
            session,
            task_description="anything",
            expected_input_tokens=1_000_000,
            expected_output_tokens=1_000_000,
        )
    alt_models = [a.model for a in r.alternatives]
    # gpt-mid-2025-08-07 must NOT appear (gpt-mid already represents the family).
    assert "gpt-mid-2025-08-07" not in alt_models


def test_recommend_alternatives_pick_distinct_families_in_order(
    family_db: Path,
) -> None:
    """With qwen-foo as the chosen row, the alternatives should be
    `qwen-bar` (next-cheapest different family) and `gpt-mid` (next
    after that). Pins the family-aware walk order."""
    with get_session() as session:
        r = recommend(
            session,
            task_description="anything",
            expected_input_tokens=1_000_000,
            expected_output_tokens=1_000_000,
        )
    alt_models = [a.model for a in r.alternatives]
    assert alt_models == ["qwen-bar", "gpt-mid"]


# --- tie reasoning -------------------------------------------------------


@pytest.fixture
def tied_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """DB where the cheapest row ties with two distinct-family rows.

    Three families (alpha, beta, gamma) at identical $1/M+$2/M. Picked
    = alpha-1 (alphabetical), tied_distinct_families = 2. Plus an
    alpha-1-2025-01-01 alias variant — must NOT count as a distinct-
    family tie since it's the same family as alpha-1.
    """
    db = tmp_path / "usage.db"
    monkeypatch.setenv("LLM_USAGE_DB_URL", f"sqlite:///{db}")
    migrate_to_head()
    with get_session() as session:
        upsert_pricing(
            session,
            [
                Pricing("provider-a", "alpha-1", 1.0, 2.0, fetched_at=1),
                Pricing("provider-a", "alpha-1-2025-01-01", 1.0, 2.0, fetched_at=1),
                Pricing("provider-b", "beta-1", 1.0, 2.0, fetched_at=1),
                Pricing("provider-c", "gamma-1", 1.0, 2.0, fetched_at=1),
            ],
        )
        session.commit()
    return db


def test_recommend_reasoning_surfaces_tie_count(tied_db: Path) -> None:
    """When the chosen row ties with 2 other distinct-family rows,
    the reasoning should say `Tied with 2 other models at $X.XXXX`."""
    with get_session() as session:
        r = recommend(
            session,
            task_description="anything",
            expected_input_tokens=1_000_000,
            expected_output_tokens=1_000_000,
        )
    assert "Tied with 2 other models" in r.reasoning
    assert "picked alphabetically" in r.reasoning


def test_recommend_reasoning_tie_count_singular_form(tied_db: Path) -> None:
    """When the tie count is exactly 1, the reasoning should use the
    singular `1 other model` (not `1 other models`)."""
    with get_session() as session:
        r = recommend(
            session,
            task_description="anything",
            expected_input_tokens=1_000_000,
            expected_output_tokens=1_000_000,
            # Restrict to two families so we get exactly one tie.
            models=["alpha-1", "beta-1"],
        )
    assert "Tied with 1 other model at" in r.reasoning


def test_recommend_reasoning_tie_count_excludes_alias_variants(
    tied_db: Path,
) -> None:
    """alpha-1 and alpha-1-2025-01-01 are aliases — they must NOT
    count as a 'tied other model.' Only beta-1 and gamma-1 should
    register as distinct-family ties."""
    with get_session() as session:
        r = recommend(
            session,
            task_description="anything",
            expected_input_tokens=1_000_000,
            expected_output_tokens=1_000_000,
        )
    # Distinct ties = beta-1, gamma-1 → 2. NOT 3.
    assert "Tied with 2 other models" in r.reasoning


def test_recommend_reasoning_no_tie_note_when_unique_winner(
    priced_db: Path,
) -> None:
    """When the chosen row's price is uniquely cheapest, no tie note
    should appear — `Tied with` must not show up in the reasoning."""
    with get_session() as session:
        r = recommend(
            session,
            task_description="anything",
            expected_input_tokens=1_000_000,
            expected_output_tokens=1_000_000,
        )
    assert "Tied with" not in r.reasoning


# --- optional task_description -------------------------------------------


def test_recommend_omitting_task_returns_no_task_reasoning(priced_db: Path) -> None:
    """`task_description=None` (the new default) yields reasoning that
    opens with 'Recommending' — no `For task '…':` prefix."""
    with get_session() as session:
        r = recommend(session)  # no task_description, no other args
    assert r.reasoning.startswith("Recommending ")
    assert "For task" not in r.reasoning


def test_recommend_omitting_task_with_budget_opens_with_budget_clause(
    priced_db: Path,
) -> None:
    """No task + budget should open `Within a $X budget: recommending …`
    — clean grammar without the task scaffolding."""
    with get_session() as session:
        r = recommend(
            session,
            expected_input_tokens=1_000_000,
            expected_output_tokens=1_000_000,
            budget_usd=5.0,
        )
    assert r.reasoning.startswith("Within a $5.0000 budget: recommending ")


def test_recommend_omitting_task_over_budget_opens_with_no_priced_model(
    priced_db: Path,
) -> None:
    """No task + over-budget should drop the task scaffolding from the
    over-budget fallback message too — opens with 'No priced model…'."""
    with get_session() as session:
        r = recommend(
            session,
            expected_input_tokens=1_000_000,
            expected_output_tokens=1_000_000,
            budget_usd=0.0001,
        )
    assert r.reasoning.startswith("No priced model fits a $0.0001 budget")


def test_recommend_task_with_internal_capitals_is_preserved(priced_db: Path) -> None:
    """Regression: `str.capitalize()` would lowercase trailing chars
    in the task description (`'GPT-5 eval'` → `'gpt-5 eval'`). Our
    `_lead_cap` helper avoids this; pin the behavior."""
    with get_session() as session:
        r = recommend(
            session,
            task_description="GPT-5 internal eval",
            expected_input_tokens=1_000_000,
            expected_output_tokens=1_000_000,
        )
    # The internal capitals must survive verbatim into the reasoning.
    assert "GPT-5 internal eval" in r.reasoning


def test_recommend_task_with_internal_capitals_preserved_with_budget(
    priced_db: Path,
) -> None:
    """Same guarantee under the budget path (which goes through
    `_normal_opener` → `_lead_cap`)."""
    with get_session() as session:
        r = recommend(
            session,
            task_description="GPT-5 vs Claude-4-7 bake-off",
            expected_input_tokens=1_000_000,
            expected_output_tokens=1_000_000,
            budget_usd=5.0,
        )
    assert "GPT-5 vs Claude-4-7 bake-off" in r.reasoning
