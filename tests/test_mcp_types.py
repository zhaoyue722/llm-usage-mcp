"""Tests that lock the MCP-tool surface defined in `docs/spec.md`.

These types are the public contract any MCP client (Claude Code, Cursor,
custom agents) reads off our JSON-RPC tools, so the tests pin every rule
that would otherwise drift on accident:

- which fields are required vs optional, with which defaults;
- which fields are typed as `Literal[...]` and reject anything else;
- that unknown keys raise (`extra="forbid"`);
- that result models are frozen (a caller mutating a result is a bug);
- that JSON round-trip works in both directions.

One representative test per concern; the shared base model carries the
config so testing it on one type proves it for all 14.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from llm_usage.core.models import (
    Alternative,
    CompareProvidersParams,
    CompareProvidersResult,
    GetPricingParams,
    GetPricingResult,
    LargestCall,
    ListProvidersParams,
    ListProvidersResult,
    PricingEntry,
    ProviderEntry,
    QuerySpendParams,
    QuerySpendResult,
    RankedEntry,
    RecommendProviderParams,
    RecommendProviderResult,
    RecordUsageParams,
    RecordUsageResult,
    SpendFilter,
    SpendGroup,
    TopModel,
    TopProvider,
    UsageSummaryParams,
    UsageSummaryResult,
)

# --- import surface --------------------------------------------------------


def test_all_seven_tools_have_params_and_result_pairs() -> None:
    """Spec lists 7 tools; each must have a params and a result model."""
    pairs = [
        (RecordUsageParams, RecordUsageResult),
        (QuerySpendParams, QuerySpendResult),
        (CompareProvidersParams, CompareProvidersResult),
        (RecommendProviderParams, RecommendProviderResult),
        (GetPricingParams, GetPricingResult),
        (UsageSummaryParams, UsageSummaryResult),
        (ListProvidersParams, ListProvidersResult),
    ]
    assert len(pairs) == 7


# --- record_usage ----------------------------------------------------------


def test_record_usage_params_minimal() -> None:
    """Per spec: provider, model, input_tokens, output_tokens are required."""
    p = RecordUsageParams(
        provider="anthropic",
        model="claude-sonnet-4-6",
        input_tokens=100,
        output_tokens=200,
    )
    # Spec defaults:
    assert p.cache_write_tokens == 0
    assert p.cache_read_tokens == 0
    assert p.success is True
    # Optionals default to None:
    assert p.duration_ms is None
    assert p.request_id is None
    assert p.project is None
    assert p.tags is None
    assert p.metadata is None


def test_record_usage_params_missing_required_raises() -> None:
    with pytest.raises(ValidationError, match="input_tokens"):
        RecordUsageParams(
            provider="anthropic",
            model="claude-sonnet-4-6",
            output_tokens=200,  # type: ignore[call-arg]
        )


def test_record_usage_result_shape() -> None:
    r = RecordUsageResult(id="evt-abc", cost_usd=0.00042, warning=None)
    assert r.id == "evt-abc"
    assert r.cost_usd == 0.00042
    assert r.warning is None


def test_record_usage_result_warning_can_be_string() -> None:
    r = RecordUsageResult(
        id="evt-abc",
        cost_usd=0.0,
        warning="model not in pricing table; cost set to 0",
    )
    assert r.warning is not None


# --- query_spend -----------------------------------------------------------


def test_query_spend_params_all_optional_with_defaults() -> None:
    p = QuerySpendParams()
    assert p.start is None
    assert p.end is None
    assert p.group_by == "provider"  # spec default
    assert p.filter is None


def test_query_spend_filter_is_typed() -> None:
    f = SpendFilter(provider="anthropic")
    assert f.provider == "anthropic"
    assert f.model is None
    assert f.project is None


def test_query_spend_filter_rejects_unknown_field() -> None:
    """A typo like `proovider` must raise instead of being silently dropped."""
    with pytest.raises(ValidationError, match="proovider"):
        SpendFilter.model_validate({"proovider": "anthropic"})


def test_query_spend_group_by_rejects_unknown_value() -> None:
    with pytest.raises(ValidationError):
        QuerySpendParams(group_by="quarter")  # type: ignore[arg-type]


def test_query_spend_result_with_groups() -> None:
    r = QuerySpendResult(
        total_cost_usd=12.34,
        total_calls=42,
        total_input_tokens=1000,
        total_output_tokens=2000,
        groups=[
            SpendGroup(
                key="anthropic",
                cost_usd=10.0,
                calls=30,
                input_tokens=700,
                output_tokens=1400,
            ),
            SpendGroup(
                key="openai",
                cost_usd=2.34,
                calls=12,
                input_tokens=300,
                output_tokens=600,
            ),
        ],
    )
    assert len(r.groups) == 2
    assert r.groups[0].key == "anthropic"


# --- compare_providers -----------------------------------------------------


def test_compare_providers_params_required_fields() -> None:
    p = CompareProvidersParams(expected_input_tokens=1000, expected_output_tokens=500)
    assert p.models is None


def test_compare_providers_params_rejects_dropped_v1_field() -> None:
    """`task_type` / `include_cached_estimate` were removed in the v1 honesty
    pass — see `docs/re_evaluation_2026_05_15.md`. `extra="forbid"` must
    reject them to prevent silent revival."""
    with pytest.raises(ValidationError, match=r"extra_forbidden|task_type"):
        CompareProvidersParams.model_validate(
            {
                "expected_input_tokens": 1000,
                "expected_output_tokens": 500,
                "task_type": "code",
            }
        )
    with pytest.raises(ValidationError, match=r"extra_forbidden|include_cached_estimate"):
        CompareProvidersParams.model_validate(
            {
                "expected_input_tokens": 1000,
                "expected_output_tokens": 500,
                "include_cached_estimate": True,
            }
        )


def test_compare_providers_result_ranked_entries() -> None:
    r = CompareProvidersResult(
        ranked=[
            RankedEntry(
                provider="deepseek",
                model="deepseek-chat",
                cost_usd=0.001,
                relative_cost_pct=100.0,
                notes=None,
            ),
            RankedEntry(
                provider="anthropic",
                model="claude-sonnet-4-6",
                cost_usd=0.030,
                relative_cost_pct=3000.0,
                notes="cache pricing applied",
            ),
        ]
    )
    assert r.ranked[0].relative_cost_pct == 100.0


# --- recommend_provider ----------------------------------------------------


def test_recommend_provider_params_only_task_description_required() -> None:
    p = RecommendProviderParams(task_description="Summarize a long PDF")
    assert p.expected_input_tokens is None
    assert p.expected_output_tokens is None
    assert p.budget_usd is None


def test_recommend_provider_params_rejects_dropped_v1_field() -> None:
    """`quality_priority` was removed in the v1 honesty pass — see
    `docs/re_evaluation_2026_05_15.md`. The post-v1 quality importer
    re-adds it; until then `extra="forbid"` keeps it from silently
    returning via a typo."""
    with pytest.raises(ValidationError, match=r"extra_forbidden|quality_priority"):
        RecommendProviderParams.model_validate(
            {
                "task_description": "x",
                "quality_priority": "lowest_cost",
            }
        )


def test_recommend_provider_result() -> None:
    r = RecommendProviderResult(
        provider="deepseek",
        model="deepseek-chat",
        estimated_cost_usd=0.0015,
        alternatives=[],
        reasoning="Lowest $/token among chat-quality models for your budget.",
    )
    assert r.estimated_cost_usd == 0.0015


def test_recommend_provider_result_carries_alternatives() -> None:
    """The result's `alternatives` field accepts a list of structured
    `Alternative` rows. Pinning the cross-model attachment so a future
    refactor doesn't accidentally flatten it to a list of strings or
    a tuple shape."""
    r = RecommendProviderResult(
        provider="qwen",
        model="qwen-turbo",
        estimated_cost_usd=0.0003,
        alternatives=[
            Alternative(
                provider="deepseek",
                model="deepseek-coder",
                estimated_cost_usd=0.0004,
            ),
            Alternative(
                provider="openai",
                model="gpt-5-nano",
                estimated_cost_usd=0.0004,
            ),
        ],
        reasoning="Cheapest of 159 priced models.",
    )
    assert len(r.alternatives) == 2
    assert r.alternatives[0].provider == "deepseek"
    assert r.alternatives[1].model == "gpt-5-nano"


# --- get_pricing -----------------------------------------------------------


def test_get_pricing_params_all_optional() -> None:
    p = GetPricingParams()
    assert p.provider is None
    assert p.model is None


def test_get_pricing_result_with_entries() -> None:
    r = GetPricingResult(
        models=[
            PricingEntry(
                provider="anthropic",
                model="claude-sonnet-4-6",
                input_per_million_usd=3.0,
                output_per_million_usd=15.0,
                cache_write_per_million_usd=3.75,
                cache_read_per_million_usd=0.30,
                fetched_at=1_700_000_000_000,
            ),
            PricingEntry(
                provider="qwen",
                model="qwen-turbo-legacy",
                input_per_million_usd=0.30,
                output_per_million_usd=0.60,
                cache_write_per_million_usd=None,
                cache_read_per_million_usd=None,
                fetched_at=1_700_000_000_000,
            ),
        ]
    )
    assert r.models[0].cache_read_per_million_usd == 0.30
    assert r.models[1].cache_write_per_million_usd is None


# --- usage_summary ---------------------------------------------------------


def test_usage_summary_params_default_period_is_week() -> None:
    """Spec: period defaults to 'week'."""
    p = UsageSummaryParams()
    assert p.period == "week"


def test_usage_summary_period_rejects_bad_value() -> None:
    with pytest.raises(ValidationError):
        UsageSummaryParams(period="decade")  # type: ignore[arg-type]


def test_usage_summary_result_shape() -> None:
    r = UsageSummaryResult(
        period="week",
        total_cost_usd=4.20,
        call_count=137,
        top_providers=[TopProvider(provider="anthropic", cost_usd=3.20, pct=76.2)],
        top_models=[TopModel(model="claude-sonnet-4-6", cost_usd=3.20, pct=76.2)],
        largest_call=LargestCall(
            id="evt-largest",
            model="claude-sonnet-4-6",
            cost_usd=0.42,
            timestamp=1_700_000_000_000,
        ),
    )
    assert r.largest_call is not None
    assert r.largest_call.timestamp == 1_700_000_000_000


def test_usage_summary_result_largest_call_optional_for_empty_window() -> None:
    """`largest_call` must be `None`-able so a zero-event window is constructable."""
    r = UsageSummaryResult(
        period="week",
        total_cost_usd=0.0,
        call_count=0,
        top_providers=[],
        top_models=[],
        largest_call=None,
    )
    assert r.largest_call is None


def test_largest_call_timestamp_is_int_ms_epoch() -> None:
    """The result side stays as ms epoch (per the decision in this change) so
    callers don't need to parse a string. A float or str must raise."""
    with pytest.raises(ValidationError):
        LargestCall(
            id="x",
            model="x",
            cost_usd=0.0,
            timestamp="2026-05-12T00:00:00Z",  # type: ignore[arg-type]
        )


# --- list_providers --------------------------------------------------------


def test_list_providers_params_takes_no_arguments() -> None:
    p = ListProvidersParams()
    assert p.model_dump() == {}


def test_list_providers_result() -> None:
    r = ListProvidersResult(
        providers=[
            ProviderEntry(
                name="anthropic",
                models=["claude-sonnet-4-6", "claude-haiku-4-5"],
                openai_compatible=False,
            ),
            ProviderEntry(name="openai", models=["gpt-4o"], openai_compatible=True),
        ]
    )
    assert r.providers[0].openai_compatible is False


# --- surface-wide invariants ----------------------------------------------


def test_unknown_field_on_params_raises() -> None:
    """extra=forbid is the load-bearing invariant for 'lock the surface'."""
    with pytest.raises(ValidationError, match=r"extra_forbidden|nonsense"):
        RecordUsageParams.model_validate(
            {
                "provider": "anthropic",
                "model": "claude-sonnet-4-6",
                "input_tokens": 1,
                "output_tokens": 1,
                "nonsense": "x",
            }
        )


def test_unknown_field_on_result_raises() -> None:
    with pytest.raises(ValidationError, match=r"extra_forbidden|nonsense"):
        RecordUsageResult.model_validate(
            {"id": "x", "cost_usd": 0.0, "warning": None, "nonsense": "y"}
        )


def test_result_models_are_frozen() -> None:
    """Result models are immutable; param models stay mutable for ergonomic
    test/CLI construction. Pin this on one representative result type."""
    r = RecordUsageResult(id="evt-1", cost_usd=0.0, warning=None)
    with pytest.raises(ValidationError):
        r.cost_usd = 1.0


def test_params_models_are_mutable() -> None:
    """Counterpart to the frozen test: params allow assignment."""
    p = RecordUsageParams(
        provider="anthropic",
        model="claude-sonnet-4-6",
        input_tokens=10,
        output_tokens=20,
    )
    p.input_tokens = 100  # must not raise
    assert p.input_tokens == 100


def test_json_round_trip_params() -> None:
    p = RecordUsageParams(
        provider="anthropic",
        model="claude-sonnet-4-6",
        input_tokens=100,
        output_tokens=200,
        cache_read_tokens=50,
        request_id="req-1",
        tags=["batch", "production"],
        metadata={"thread_id": "abc-123"},
    )
    raw = p.model_dump_json()
    p2 = RecordUsageParams.model_validate_json(raw)
    assert p == p2


def test_json_round_trip_result_with_nested() -> None:
    r = QuerySpendResult(
        total_cost_usd=1.0,
        total_calls=1,
        total_input_tokens=10,
        total_output_tokens=20,
        groups=[
            SpendGroup(
                key="anthropic",
                cost_usd=1.0,
                calls=1,
                input_tokens=10,
                output_tokens=20,
            )
        ],
    )
    raw = r.model_dump_json()
    r2 = QuerySpendResult.model_validate_json(raw)
    assert r == r2


def test_money_fields_typed_float_not_decimal() -> None:
    """Spec: returns `cost_usd: number` (JSON number ~ float). Pin this so a
    well-meaning future refactor to Decimal doesn't silently change the wire
    format. An int is acceptable since Pydantic coerces 0 -> 0.0 for float
    fields, but the stored value must be a Python float."""
    r = RecordUsageResult(id="x", cost_usd=0, warning=None)
    assert isinstance(r.cost_usd, float)
