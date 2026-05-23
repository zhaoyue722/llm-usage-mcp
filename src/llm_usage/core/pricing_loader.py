"""Convert the vendored LiteLLM pricing JSON into internal `Pricing` records.

`pricing_data/prices.json` is committed in LiteLLM's verbatim shape:
per-token costs, `litellm_provider` keys (e.g., `dashscope`), and
`tiered_pricing` lists for some Qwen models. This module owns the
one-time conversion at load time so the rest of the codebase only ever
sees our `Pricing` dataclass (per-million-USD rates, our provider names).

Conversions applied:

- **Provider rename**: LiteLLM uses `dashscope` (the API endpoint name);
  we surface it as `qwen` (the model family) since that's how users
  think about it. Other providers pass through unchanged.
- **Model name**: LiteLLM keys are sometimes namespaced (`dashscope/qwen-coder`)
  and sometimes bare (`claude-haiku-4-5`). We strip `{litellm_provider}/`
  when present so model names are clean.
- **Units**: per-token USD * 1e6 = per-million USD.
- **Cache fields**: `cache_creation_input_token_cost` → `cache_write_*`,
  `cache_read_input_token_cost` → `cache_read_*`. Either may be absent
  (OpenAI/DeepSeek absorb the write cost into input); absent → `None`.
- **Tiered pricing**: a few Qwen models only have `tiered_pricing` and
  no flat rates. The first tier is written into the flat
  `input_per_million_usd` / `output_per_million_usd` fields as a
  fallback (so cost code that doesn't yet read tiers keeps working),
  *and* the full tier list is attached to `Pricing.tiers`. The
  `upsert_pricing` path then materializes every tier into the
  `pricing_tier` table.
- **Duplicates**: LiteLLM occasionally keeps both a bare and a
  namespaced entry for the same model (e.g., `deepseek-chat` and
  `deepseek/deepseek-chat`). After our model-name normalization they
  collide on `(provider, model)`. We prefer whichever entry has more
  cache rates populated — the namespaced form is usually the more
  current/complete record.
"""

from __future__ import annotations

import json
import time
from importlib.resources import files
from typing import Any

from llm_usage.core.pricing import Pricing, Tier

# LiteLLM provider key -> our provider name. Anything not in this map is
# silently skipped by `parse_litellm_entry` — keeps the loader inert
# against future LiteLLM additions outside our v1 scope.
_PROVIDER_MAP: dict[str, str] = {
    "anthropic": "anthropic",
    "openai": "openai",
    "deepseek": "deepseek",
    "dashscope": "qwen",
}

# per-token USD * 1e6 = per-million USD.
_PER_MILLION = 1_000_000


def load_vendored_pricing(*, fetched_at: int | None = None) -> list[Pricing]:
    """Load the bundled `prices.json` and return one `Pricing` per (provider, model).

    `fetched_at` is the millisecond epoch stamped onto every record;
    defaults to "now". Pass an explicit value in tests for determinism.
    """
    text = files("llm_usage.core.pricing_data").joinpath("prices.json").read_text()
    data: dict[str, dict[str, Any]] = json.loads(text)
    return _convert_all(data, fetched_at=fetched_at)


def _convert_all(
    data: dict[str, dict[str, Any]],
    *,
    fetched_at: int | None,
) -> list[Pricing]:
    ts = fetched_at if fetched_at is not None else int(time.time() * 1000)
    by_key: dict[tuple[str, str], Pricing] = {}
    for model_key, entry in data.items():
        pricing = parse_litellm_entry(model_key, entry, fetched_at=ts)
        if pricing is None:
            continue
        key = (pricing.provider, pricing.model)
        existing = by_key.get(key)
        if existing is None or _cache_field_count(pricing) > _cache_field_count(existing):
            by_key[key] = pricing
    return list(by_key.values())


def parse_litellm_entry(
    model_key: str,
    entry: dict[str, Any],
    *,
    fetched_at: int | None = None,
) -> Pricing | None:
    """Convert one LiteLLM entry to a `Pricing`, or `None` if unsupported.

    Returns `None` when:
    - `litellm_provider` is missing or outside our v1 set, or
    - the entry has neither flat rates nor a usable `tiered_pricing`.
    """
    litellm_provider = entry.get("litellm_provider")
    if not isinstance(litellm_provider, str):
        return None
    provider = _PROVIDER_MAP.get(litellm_provider)
    if provider is None:
        return None

    rates = _resolve_input_output(entry)
    if rates is None:
        return None
    input_per_token, output_per_token = rates

    cache_write = entry.get("cache_creation_input_token_cost")
    cache_read = entry.get("cache_read_input_token_cost")
    tiers = _extract_tiers(entry)

    return Pricing(
        provider=provider,
        model=_strip_provider_prefix(model_key, litellm_provider),
        input_per_million_usd=input_per_token * _PER_MILLION,
        output_per_million_usd=output_per_token * _PER_MILLION,
        cache_write_per_million_usd=(
            cache_write * _PER_MILLION if isinstance(cache_write, int | float) else None
        ),
        cache_read_per_million_usd=(
            cache_read * _PER_MILLION if isinstance(cache_read, int | float) else None
        ),
        fetched_at=fetched_at,
        tiers=tuple(tiers),
    )


def _strip_provider_prefix(model_key: str, litellm_provider: str) -> str:
    prefix = f"{litellm_provider}/"
    if model_key.startswith(prefix):
        return model_key[len(prefix) :]
    return model_key


def _resolve_input_output(entry: dict[str, Any]) -> tuple[float, float] | None:
    """Pick the base (input, output) per-token rate, preferring flat over tiered.

    Flat rates win when present. If only `tiered_pricing` is given (some
    Qwen models), use the first tier as the base rate — this matches the
    LiteLLM convention and is what the README locks in for v1.
    """
    flat_in = entry.get("input_cost_per_token")
    flat_out = entry.get("output_cost_per_token")
    if isinstance(flat_in, int | float) and isinstance(flat_out, int | float):
        return float(flat_in), float(flat_out)

    tiers = entry.get("tiered_pricing")
    if isinstance(tiers, list) and tiers:
        first = tiers[0]
        if isinstance(first, dict):
            tier_in = first.get("input_cost_per_token")
            tier_out = first.get("output_cost_per_token")
            if isinstance(tier_in, int | float) and isinstance(tier_out, int | float):
                return float(tier_in), float(tier_out)
    return None


def _cache_field_count(pricing: Pricing) -> int:
    """Count of populated cache rates — used to break duplicate-key ties."""
    return int(pricing.cache_write_per_million_usd is not None) + int(
        pricing.cache_read_per_million_usd is not None
    )


def _extract_tiers(entry: dict[str, Any]) -> list[Tier]:
    """Parse the LiteLLM `tiered_pricing` array into `Tier` dataclasses.

    Each tier is `{range: [start, end], input_cost_per_token, output_cost_per_token}`
    in the upstream JSON. We turn that into a `Tier` with per-million
    rates and an explicit `tier_index` (0, 1, 2, ...). Malformed tiers
    are skipped silently rather than failing the whole entry —
    consistent with the rest of the loader's defensive parsing.

    Returns an empty list when the entry has no `tiered_pricing` field
    or all of its tiers are malformed. Flat-rate models — the
    overwhelming majority — get no tier rows.
    """
    tiers = entry.get("tiered_pricing")
    if not isinstance(tiers, list) or not tiers:
        return []
    out: list[Tier] = []
    for idx, t in enumerate(tiers):
        if not isinstance(t, dict):
            continue
        rng = t.get("range")
        # LiteLLM emits `[start, end]` with the values either int or
        # float (e.g. `1000000.0`). Coerce to int; reject if the shape
        # doesn't match — partial tier data isn't worth keeping.
        if not isinstance(rng, list) or len(rng) != 2:
            continue
        try:
            start = int(rng[0])
            end = int(rng[1])
        except (TypeError, ValueError):
            continue
        tier_in = t.get("input_cost_per_token")
        tier_out = t.get("output_cost_per_token")
        if not isinstance(tier_in, int | float) or not isinstance(tier_out, int | float):
            continue
        out.append(
            Tier(
                tier_index=idx,
                range_start=start,
                range_end=end,
                input_per_million_usd=float(tier_in) * _PER_MILLION,
                output_per_million_usd=float(tier_out) * _PER_MILLION,
            )
        )
    return out
