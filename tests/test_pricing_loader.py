"""Tests for the LiteLLM-JSON → `Pricing` loader.

Two layers:

1. Unit tests on `parse_litellm_entry` with hand-crafted entries — pin
   each conversion rule (provider rename, prefix strip, units, cache
   field mapping, tiered fallback, skip rules) in isolation.
2. End-to-end on the real vendored `prices.json` via `load_vendored_pricing`
   — catches refresh-time breakage where the file shape drifts but the
   unit tests still pass.
"""

from __future__ import annotations

import pytest

from llm_usage.core import (
    Pricing,
    Tier,
    load_vendored_pricing,
    parse_litellm_entry,
)

# A realistic Anthropic entry shape (fields the loader cares about plus
# a couple it should ignore).
_ANTHROPIC_ENTRY = {
    "litellm_provider": "anthropic",
    "mode": "chat",
    "input_cost_per_token": 0.000003,
    "output_cost_per_token": 0.000015,
    "cache_creation_input_token_cost": 0.00000375,
    "cache_read_input_token_cost": 3e-7,
    "supports_prompt_caching": True,
}

# OpenAI: cache_read only (cache write is absorbed into input).
_OPENAI_ENTRY = {
    "litellm_provider": "openai",
    "mode": "chat",
    "input_cost_per_token": 0.0000025,
    "output_cost_per_token": 0.00001,
    "cache_read_input_token_cost": 0.00000125,
}

# DeepSeek: same shape as OpenAI for cache.
_DEEPSEEK_ENTRY = {
    "litellm_provider": "deepseek",
    "mode": "chat",
    "input_cost_per_token": 2.8e-7,
    "output_cost_per_token": 4.2e-7,
    "cache_read_input_token_cost": 2.8e-8,
}

# Qwen flat-rate entry (no caching).
_QWEN_FLAT_ENTRY = {
    "litellm_provider": "dashscope",
    "mode": "chat",
    "input_cost_per_token": 3e-7,
    "output_cost_per_token": 0.0000015,
}

# Qwen tiered-pricing entry (no flat rates at top level).
_QWEN_TIERED_ENTRY = {
    "litellm_provider": "dashscope",
    "mode": "chat",
    "tiered_pricing": [
        {
            "input_cost_per_token": 5e-8,
            "output_cost_per_token": 4e-7,
            "range": [0, 256000.0],
        },
        {
            "input_cost_per_token": 2.5e-7,
            "output_cost_per_token": 0.000002,
            "range": [256000.0, 1000000.0],
        },
    ],
}


# --- parse_litellm_entry: provider mapping ---------------------------------


def test_anthropic_provider_passes_through() -> None:
    p = parse_litellm_entry("claude-sonnet-4-5", _ANTHROPIC_ENTRY, fetched_at=42)
    assert p is not None
    assert p.provider == "anthropic"
    assert p.model == "claude-sonnet-4-5"


def test_dashscope_provider_renamed_to_qwen() -> None:
    p = parse_litellm_entry("dashscope/qwen-coder", _QWEN_FLAT_ENTRY, fetched_at=42)
    assert p is not None
    assert p.provider == "qwen"
    assert p.model == "qwen-coder"  # provider prefix stripped


def test_unknown_provider_returns_none() -> None:
    entry = {**_OPENAI_ENTRY, "litellm_provider": "bedrock"}
    assert parse_litellm_entry("bedrock/foo", entry) is None


def test_missing_provider_returns_none() -> None:
    entry = {k: v for k, v in _OPENAI_ENTRY.items() if k != "litellm_provider"}
    assert parse_litellm_entry("foo", entry) is None


# --- parse_litellm_entry: model name normalization -------------------------


def test_namespaced_model_key_strips_prefix() -> None:
    p = parse_litellm_entry("deepseek/deepseek-chat", _DEEPSEEK_ENTRY)
    assert p is not None
    assert p.model == "deepseek-chat"


def test_bare_model_key_kept_as_is() -> None:
    p = parse_litellm_entry("deepseek-chat", _DEEPSEEK_ENTRY)
    assert p is not None
    assert p.model == "deepseek-chat"


def test_only_litellm_provider_prefix_is_stripped() -> None:
    """A `/` inside the model name itself should survive (no real example today,
    but the logic should only touch the documented prefix)."""
    entry = {**_OPENAI_ENTRY}
    p = parse_litellm_entry("ft:gpt-4o:my-org/internal", entry)
    assert p is not None
    assert p.model == "ft:gpt-4o:my-org/internal"


# --- parse_litellm_entry: per-token → per-million conversion ---------------


def test_input_output_rates_scaled_to_per_million() -> None:
    p = parse_litellm_entry("claude-sonnet-4-5", _ANTHROPIC_ENTRY)
    assert p is not None
    # 0.000003 USD/token * 1e6 = $3.00 / M
    assert p.input_per_million_usd == 3.0
    # 0.000015 USD/token * 1e6 = $15.00 / M
    assert p.output_per_million_usd == 15.0


def test_anthropic_cache_rates_present_and_scaled() -> None:
    p = parse_litellm_entry("claude-sonnet-4-5", _ANTHROPIC_ENTRY)
    assert p is not None
    assert p.cache_write_per_million_usd == 3.75
    assert p.cache_read_per_million_usd == 0.30


def test_openai_has_only_cache_read() -> None:
    """OpenAI/DeepSeek absorb cache write into input → write rate must be None."""
    p = parse_litellm_entry("gpt-4o", _OPENAI_ENTRY)
    assert p is not None
    assert p.cache_write_per_million_usd is None
    assert p.cache_read_per_million_usd == 1.25


def test_qwen_with_no_caching_has_both_cache_rates_none() -> None:
    p = parse_litellm_entry("dashscope/qwen-coder", _QWEN_FLAT_ENTRY)
    assert p is not None
    assert p.cache_write_per_million_usd is None
    assert p.cache_read_per_million_usd is None


# --- parse_litellm_entry: tiered pricing -----------------------------------


def test_tiered_pricing_uses_first_tier_as_base() -> None:
    p = parse_litellm_entry("dashscope/qwen-flash", _QWEN_TIERED_ENTRY)
    assert p is not None
    # First tier: 5e-8 in / 4e-7 out → $0.05/M in, $0.40/M out.
    # Use approx because per-token JSON values * 1e6 don't always round-trip
    # exactly through float (5e-8 * 1e6 = 0.04999...).
    assert p.input_per_million_usd == pytest.approx(0.05)
    assert p.output_per_million_usd == pytest.approx(0.40)


def test_tiered_pricing_populates_all_tiers_on_pricing_object() -> None:
    """The flat base rate is tier-0; `pricing.tiers` carries every tier in order."""
    p = parse_litellm_entry("dashscope/qwen-flash", _QWEN_TIERED_ENTRY)
    assert p is not None
    assert len(p.tiers) == 2
    # Tier 0
    assert p.tiers[0].tier_index == 0
    assert p.tiers[0].range_start == 0
    assert p.tiers[0].range_end == 256_000
    assert p.tiers[0].input_per_million_usd == pytest.approx(0.05)
    assert p.tiers[0].output_per_million_usd == pytest.approx(0.40)
    # Tier 1
    assert p.tiers[1].tier_index == 1
    assert p.tiers[1].range_start == 256_000
    assert p.tiers[1].range_end == 1_000_000
    assert p.tiers[1].input_per_million_usd == pytest.approx(0.25)
    assert p.tiers[1].output_per_million_usd == pytest.approx(2.0)


def test_flat_rates_win_over_tiered_when_both_present() -> None:
    """If a future LiteLLM entry happens to carry both, the flat rate is the
    canonical 'base' and should be picked. (Defensive — no real example today.)"""
    entry = {**_QWEN_TIERED_ENTRY, "input_cost_per_token": 1e-6, "output_cost_per_token": 2e-6}
    p = parse_litellm_entry("dashscope/qwen-mixed", entry)
    assert p is not None
    assert p.input_per_million_usd == 1.0
    assert p.output_per_million_usd == 2.0


def test_no_flat_no_tiered_returns_none() -> None:
    entry = {"litellm_provider": "openai", "mode": "chat"}
    assert parse_litellm_entry("ghost", entry) is None


def test_empty_tiered_list_returns_none() -> None:
    entry = {"litellm_provider": "dashscope", "mode": "chat", "tiered_pricing": []}
    assert parse_litellm_entry("dashscope/empty", entry) is None


# --- parse_litellm_entry: fetched_at ---------------------------------------


def test_fetched_at_is_propagated() -> None:
    p = parse_litellm_entry("claude-sonnet-4-5", _ANTHROPIC_ENTRY, fetched_at=1_700_000_000_000)
    assert p is not None
    assert p.fetched_at == 1_700_000_000_000


def test_fetched_at_defaults_to_none_when_omitted() -> None:
    p = parse_litellm_entry("claude-sonnet-4-5", _ANTHROPIC_ENTRY)
    assert p is not None
    assert p.fetched_at is None


# --- load_vendored_pricing: end-to-end on the real file --------------------


def test_load_vendored_returns_many_pricings() -> None:
    """Smoke: the real file should yield well over 100 records."""
    records = load_vendored_pricing(fetched_at=1)
    assert len(records) > 100


def test_load_vendored_stamps_fetched_at_on_every_record() -> None:
    records = load_vendored_pricing(fetched_at=1_700_000_000_000)
    assert all(p.fetched_at == 1_700_000_000_000 for p in records)


def test_load_vendored_dashscope_renamed_throughout() -> None:
    records = load_vendored_pricing(fetched_at=1)
    providers = {p.provider for p in records}
    # 'qwen' replaces 'dashscope' at our boundary.
    assert "qwen" in providers
    assert "dashscope" not in providers


def test_load_vendored_covers_all_v1_providers() -> None:
    records = load_vendored_pricing(fetched_at=1)
    providers = {p.provider for p in records}
    assert {"anthropic", "openai", "deepseek", "qwen"} <= providers


def test_load_vendored_deduplicates_by_provider_model() -> None:
    """LiteLLM ships both `deepseek-chat` and `deepseek/deepseek-chat`; after
    name normalization they collide. The loader must keep exactly one."""
    records = load_vendored_pricing(fetched_at=1)
    keys = [(p.provider, p.model) for p in records]
    assert len(keys) == len(set(keys))


def test_load_vendored_deepseek_chat_kept_with_richer_cache_data() -> None:
    """The bare `deepseek-chat` entry has only `cache_read_input_token_cost`;
    the `deepseek/deepseek-chat` entry adds `cache_creation_input_token_cost`.
    Tie-breaker should pick the richer record (write rate populated)."""
    records = load_vendored_pricing(fetched_at=1)
    chat = next(p for p in records if p.provider == "deepseek" and p.model == "deepseek-chat")
    assert chat.cache_write_per_million_usd is not None
    assert chat.cache_read_per_million_usd is not None


def test_load_vendored_records_are_pricing_instances() -> None:
    records = load_vendored_pricing(fetched_at=1)
    assert all(isinstance(p, Pricing) for p in records)


def test_load_vendored_no_negative_or_nan_rates() -> None:
    """Catches a corrupted refresh that injected garbage values."""
    records = load_vendored_pricing(fetched_at=1)
    for p in records:
        assert p.input_per_million_usd >= 0
        assert p.output_per_million_usd >= 0
        if p.cache_write_per_million_usd is not None:
            assert p.cache_write_per_million_usd >= 0
        if p.cache_read_per_million_usd is not None:
            assert p.cache_read_per_million_usd >= 0


# --- tier extraction edge cases --------------------------------------------


def test_flat_only_entry_has_empty_tiers() -> None:
    """No `tiered_pricing` field → `Pricing.tiers` is an empty tuple, not None."""
    p = parse_litellm_entry("claude-sonnet-4-5", _ANTHROPIC_ENTRY)
    assert p is not None
    assert p.tiers == ()


def test_tier_with_malformed_range_is_skipped() -> None:
    """A tier whose `range` isn't a 2-element list is dropped silently.

    Defensive — keeps a single malformed tier from poisoning a model
    whose other tiers are fine. The model still gets a Pricing as long
    as the first usable tier (or a flat rate) exists.
    """
    entry = {
        "litellm_provider": "dashscope",
        "mode": "chat",
        "tiered_pricing": [
            {  # good
                "input_cost_per_token": 1e-7,
                "output_cost_per_token": 2e-7,
                "range": [0, 256000],
            },
            {  # bad: range is a string
                "input_cost_per_token": 5e-7,
                "output_cost_per_token": 1e-6,
                "range": "0-256000",
            },
            {  # good
                "input_cost_per_token": 5e-7,
                "output_cost_per_token": 1e-6,
                "range": [256000, 1000000],
            },
        ],
    }
    p = parse_litellm_entry("dashscope/qwen-mixed", entry)
    assert p is not None
    # The middle tier was malformed; tier_index preserves source ordering
    # (0 and 2 — not renumbered to 0 and 1) so the index keys still
    # match a future re-parse of the same upstream JSON.
    assert [t.tier_index for t in p.tiers] == [0, 2]


def test_tier_with_missing_rate_is_skipped() -> None:
    """A tier missing input or output cost is dropped."""
    entry = {
        "litellm_provider": "dashscope",
        "mode": "chat",
        "tiered_pricing": [
            {
                "input_cost_per_token": 1e-7,
                "output_cost_per_token": 2e-7,
                "range": [0, 256000],
            },
            {
                # missing input_cost_per_token
                "output_cost_per_token": 1e-6,
                "range": [256000, 1000000],
            },
        ],
    }
    p = parse_litellm_entry("dashscope/qwen-x", entry)
    assert p is not None
    assert len(p.tiers) == 1
    assert p.tiers[0].tier_index == 0


def test_tiered_ranges_coerce_float_to_int() -> None:
    """LiteLLM emits `1000000.0` (float); we store integers."""
    p = parse_litellm_entry("dashscope/qwen-flash", _QWEN_TIERED_ENTRY)
    assert p is not None
    for t in p.tiers:
        assert isinstance(t.range_start, int)
        assert isinstance(t.range_end, int)


def test_tier_is_a_proper_dataclass() -> None:
    """Sanity: `Tier` is constructible directly + frozen (hashable)."""
    t = Tier(
        tier_index=0,
        range_start=0,
        range_end=100,
        input_per_million_usd=1.0,
        output_per_million_usd=2.0,
    )
    assert t.tier_index == 0
    # Frozen → hashable, so it can live in sets / dict keys.
    assert hash(t) == hash(t)


def test_load_vendored_populates_tiers_for_qwen_flash() -> None:
    """End-to-end: the real prices.json has qwen-flash with two tiers."""
    records = load_vendored_pricing(fetched_at=1)
    qwen_flash = next(
        (p for p in records if p.provider == "qwen" and p.model == "qwen-flash"),
        None,
    )
    assert qwen_flash is not None
    assert len(qwen_flash.tiers) == 2
    # Tier 0 covers [0, 256k); tier 1 covers [256k, 1M).
    assert qwen_flash.tiers[0].range_start == 0
    assert qwen_flash.tiers[0].range_end == 256_000
    assert qwen_flash.tiers[1].range_start == 256_000
    assert qwen_flash.tiers[1].range_end == 1_000_000


def test_load_vendored_models_either_all_or_no_tiers() -> None:
    """No record can carry tiers without also carrying a tier-0 flat rate.

    `parse_litellm_entry` writes tier 0's rate into the flat fields as
    fallback for callers that don't read tiers. So a tiered record must
    also have non-None flat rates. (Catches a future refactor that
    accidentally decouples the two.)"""
    records = load_vendored_pricing(fetched_at=1)
    for p in records:
        if p.tiers:
            assert p.input_per_million_usd > 0
            assert p.output_per_million_usd > 0


# --- pricing_overrides.json merge ------------------------------------------


def test_merge_overrides_adds_new_key() -> None:
    """A key only in overrides shows up as a new entry."""
    from llm_usage.core.pricing_loader import _merge_overrides

    base = {"foo": {"litellm_provider": "anthropic", "input_cost_per_token": 1e-6}}
    overrides = {"bar": {"litellm_provider": "openai", "input_cost_per_token": 2e-6}}
    merged = _merge_overrides(base, overrides)
    assert merged == {
        "foo": {"litellm_provider": "anthropic", "input_cost_per_token": 1e-6},
        "bar": {"litellm_provider": "openai", "input_cost_per_token": 2e-6},
    }


def test_merge_overrides_field_level_merge_preserves_base_fields() -> None:
    """An override that touches one field leaves the base entry's other
    fields intact — the load-bearing property that lets a user write a
    minimal override without having to repeat every field."""
    from llm_usage.core.pricing_loader import _merge_overrides

    base = {
        "foo": {
            "litellm_provider": "deepseek",
            "input_cost_per_token": 2.8e-7,
            "output_cost_per_token": 4.2e-7,
            "cache_read_input_token_cost": 2.8e-8,
        }
    }
    overrides = {"foo": {"input_cost_per_token": 1e-7}}
    merged = _merge_overrides(base, overrides)
    # Only input_cost_per_token changed; output + cache_read survived.
    assert merged["foo"]["input_cost_per_token"] == 1e-7
    assert merged["foo"]["output_cost_per_token"] == 4.2e-7
    assert merged["foo"]["cache_read_input_token_cost"] == 2.8e-8
    assert merged["foo"]["litellm_provider"] == "deepseek"


def test_merge_overrides_empty_overrides_returns_base_unchanged() -> None:
    """No overrides → result equals base (the most common path)."""
    from llm_usage.core.pricing_loader import _merge_overrides

    base = {"foo": {"litellm_provider": "anthropic", "input_cost_per_token": 1e-6}}
    assert _merge_overrides(base, {}) == base


def test_merge_overrides_does_not_mutate_inputs() -> None:
    """The merge returns a new dict — callers can keep references to the
    originals."""
    from llm_usage.core.pricing_loader import _merge_overrides

    base = {"foo": {"input_cost_per_token": 1e-6}}
    overrides = {"foo": {"input_cost_per_token": 2e-6}}
    base_snapshot = {k: dict(v) for k, v in base.items()}
    overrides_snapshot = {k: dict(v) for k, v in overrides.items()}
    _merge_overrides(base, overrides)
    assert base == base_snapshot
    assert overrides == overrides_snapshot


def test_load_vendored_includes_overrides_for_deepseek_v4_flash() -> None:
    """End-to-end: the override file's deepseek-v4-flash entry shows up
    as a real Pricing after `load_vendored_pricing`."""
    records = load_vendored_pricing(fetched_at=1)
    v4_flash = next(
        (p for p in records if p.provider == "deepseek" and p.model == "deepseek-v4-flash"),
        None,
    )
    assert v4_flash is not None
    # Source: api-docs.deepseek.com (2026-05-25).
    # Input $0.14/M = 1.4e-7 USD/token; output $0.28/M = 2.8e-7 USD/token.
    assert v4_flash.input_per_million_usd == pytest.approx(0.14)
    assert v4_flash.output_per_million_usd == pytest.approx(0.28)
    # Cache hit $0.0028/M; cache creation is $0 (DeepSeek bills writes
    # at the regular input rate, no separate creation line item).
    assert v4_flash.cache_read_per_million_usd == pytest.approx(0.0028)
    assert v4_flash.cache_write_per_million_usd == 0.0


def test_load_vendored_includes_overrides_for_deepseek_v4_pro() -> None:
    """The v4-pro entry uses post-promo (steady-state) rates."""
    records = load_vendored_pricing(fetched_at=1)
    v4_pro = next(
        (p for p in records if p.provider == "deepseek" and p.model == "deepseek-v4-pro"),
        None,
    )
    assert v4_pro is not None
    # Post-promo rates (the 75%-off promo ends 2026-05-31).
    # Input $1.74/M = 1.74e-6 USD/token; output $3.48/M = 3.48e-6 USD/token.
    assert v4_pro.input_per_million_usd == pytest.approx(1.74)
    assert v4_pro.output_per_million_usd == pytest.approx(3.48)
    assert v4_pro.cache_read_per_million_usd == pytest.approx(0.0145)
