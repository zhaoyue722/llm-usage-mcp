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
