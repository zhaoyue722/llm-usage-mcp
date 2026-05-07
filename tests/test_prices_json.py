"""Structural tests for the vendored prices.json.

These don't validate prices for correctness (rates change weekly); they
catch the file going corrupt, missing a v1 provider, or a refresh
silently breaking the LiteLLM shape we depend on.
"""

from __future__ import annotations

import json
from importlib.resources import files
from typing import Any

import pytest

V1_PROVIDERS = {"anthropic", "openai", "deepseek", "dashscope"}
ALLOWED_MODES = {"chat", "responses"}


@pytest.fixture(scope="module")
def prices() -> dict[str, dict[str, Any]]:
    text = files("llm_usage.core.pricing_data").joinpath("prices.json").read_text()
    data: dict[str, dict[str, Any]] = json.loads(text)
    return data


def test_prices_file_is_non_empty(prices: dict[str, dict[str, Any]]) -> None:
    assert len(prices) > 50  # sanity: we expect ~180 models


def test_every_v1_provider_is_present(prices: dict[str, dict[str, Any]]) -> None:
    seen: set[str] = {entry["litellm_provider"] for entry in prices.values()}
    missing = V1_PROVIDERS - seen
    assert not missing, f"missing v1 providers in prices.json: {sorted(missing)}"


def test_no_unexpected_providers(prices: dict[str, dict[str, Any]]) -> None:
    """Drift guard: a refresh script that broadens the filter should fail this."""
    seen: set[str] = {entry["litellm_provider"] for entry in prices.values()}
    unexpected = seen - V1_PROVIDERS
    assert not unexpected, f"unexpected providers in prices.json: {sorted(unexpected)}"


def test_every_entry_has_required_pricing_fields(
    prices: dict[str, dict[str, Any]],
) -> None:
    """The loader needs at least the provider name and a way to get input/output rates.

    Most models expose flat `input_cost_per_token` / `output_cost_per_token`. A
    handful of Qwen (dashscope) models use `tiered_pricing` instead — a list of
    `{range, input_cost_per_token, output_cost_per_token}` entries; the loader
    will need to pick a base rate (likely the first tier).
    """
    for model, entry in prices.items():
        assert "litellm_provider" in entry, model
        has_flat = "input_cost_per_token" in entry and "output_cost_per_token" in entry
        has_tiered = "tiered_pricing" in entry
        assert has_flat or has_tiered, f"{model}: no flat or tiered pricing fields"


def test_modes_are_chat_or_responses(prices: dict[str, dict[str, Any]]) -> None:
    for model, entry in prices.items():
        mode = entry.get("mode")
        assert mode in ALLOWED_MODES, f"{model}: unexpected mode {mode!r}"


def test_anthropic_caching_models_have_both_cache_costs(
    prices: dict[str, dict[str, Any]],
) -> None:
    """When Anthropic advertises caching, both write and read costs should be present.

    OpenAI/DeepSeek often have only cache_read (they absorb the write cost),
    so we assert this only for Anthropic.
    """
    for model, entry in prices.items():
        if entry.get("litellm_provider") != "anthropic":
            continue
        if not entry.get("supports_prompt_caching"):
            continue
        assert "cache_creation_input_token_cost" in entry, model
        assert "cache_read_input_token_cost" in entry, model
