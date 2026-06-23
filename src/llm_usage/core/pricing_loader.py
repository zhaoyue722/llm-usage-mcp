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

**Overrides.** `pricing_data/pricing_overrides.json` is a small,
manually-maintained sibling that gets field-merged on top of
`prices.json` before parsing. Use cases: a model LiteLLM hasn't
catalogued yet (`deepseek-v4-flash` today), or a region whose rate
card differs from LiteLLM's tracked schedule. The merge is
field-level — an override entry that only sets `input_cost_per_token`
leaves the rest of the base entry intact. New keys add new models;
matching keys merge over the base. The file is JSON (no comments
allowed); the override schema is the same LiteLLM shape `prices.json`
uses. See `pricing_data/README.md` for the rules of thumb (overrides
should be sparing; prefer upstreaming a fix to LiteLLM when
possible). Survives the weekly refresh because the refresh script
only touches `prices.json`.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from importlib.resources import files
from typing import Any, Literal, cast

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
    """Load the bundled `prices.json` (+ optional overrides) and return one
    `Pricing` per (provider, model).

    `fetched_at` is the millisecond epoch stamped onto every record;
    defaults to "now". Pass an explicit value in tests for determinism.

    `pricing_overrides.json` is loaded and field-merged on top of
    `prices.json` before parsing — see this module's docstring for the
    merge rules.
    """
    base = _load_base()
    overrides = _load_overrides()
    data = _merge_overrides(base, overrides)
    return _convert_all(data, fetched_at=fetched_at)


def _load_base() -> dict[str, dict[str, Any]]:
    """Load the raw vendored LiteLLM snapshot (`prices.json`)."""
    text = files("llm_usage.core.pricing_data").joinpath("prices.json").read_text()
    return cast(dict[str, dict[str, Any]], json.loads(text))


def _load_overrides() -> dict[str, dict[str, Any]]:
    """Read `pricing_overrides.json` if present; return an empty dict otherwise.

    Most users won't have overrides — the absent-file path is normal.
    A *malformed* override file is the user's mistake and surfaces as
    `json.JSONDecodeError` (we don't catch it): hard-failing at boot
    is louder and easier to debug than a silently-ignored file that
    leaves recorded costs subtly wrong.
    """
    resource = files("llm_usage.core.pricing_data").joinpath("pricing_overrides.json")
    if not resource.is_file():
        return {}
    text = resource.read_text()
    if not text.strip():
        return {}
    # The override schema mirrors LiteLLM's, which is `dict[str, dict[str, Any]]`.
    # Trust the JSON shape — the `parse_litellm_entry` defensive checks
    # catch any per-entry malformedness downstream.
    return cast(dict[str, dict[str, Any]], json.loads(text))


def _merge_overrides(
    base: dict[str, dict[str, Any]],
    overrides: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Field-level merge: override fields win, untouched base fields stay.

    For a key present in both, the result is `{**base[key], **override[key]}`
    — so an override that only specifies one field (e.g.
    `{"deepseek-chat": {"input_cost_per_token": 1e-7}}`) doesn't
    accidentally drop the rest of the base entry. For a key only in
    overrides, the override entry is added as-is (used for models
    LiteLLM hasn't catalogued yet). For a key only in base, no change.

    Returns a new dict; neither argument is mutated.
    """
    merged: dict[str, dict[str, Any]] = dict(base)
    for key, override_entry in overrides.items():
        existing = merged.get(key, {})
        merged[key] = {**existing, **override_entry}
    return merged


# Cost-bearing fields the loader actually reads. Drift detection compares
# only these — an override re-stating `mode` or `source` without changing
# a price isn't meaningfully pinning anything. `_`-prefixed keys (e.g.
# `_reason`, `_added` documentation metadata) are ignored everywhere:
# `parse_litellm_entry` doesn't read them and neither does drift.
_PRICE_FIELDS: tuple[str, ...] = (
    "input_cost_per_token",
    "output_cost_per_token",
    "cache_read_input_token_cost",
    "cache_creation_input_token_cost",
)


@dataclass(frozen=True)
class OverrideDrift:
    """One override entry's standing against the LiteLLM base catalog.

    - ``redundant``: every price field the override pins already matches
      LiteLLM (or it pins no price field at all) — the override is dead
      weight and should be deleted.
    - ``diverged``: at least one price field differs from LiteLLM — an
      intentional pin. ``detail`` carries the per-field deltas so a
      reviewer can re-confirm it's still wanted.
    - ``gap_fill``: LiteLLM doesn't carry this key — the override is the
      only source, nothing to drift against.
    """

    key: str
    kind: Literal["redundant", "diverged", "gap_fill"]
    detail: str


def detect_override_drift() -> list[OverrideDrift]:
    """Compare the vendored overrides against the raw LiteLLM catalog.

    Loads the real `prices.json` + `pricing_overrides.json` and returns
    one finding per override entry. Drives the CI drift test (fail on
    ``redundant``, surface ``diverged``); safe to call anytime — empty
    overrides yield an empty list.
    """
    return _detect_drift(_load_base(), _load_overrides())


def _detect_drift(
    base: dict[str, dict[str, Any]],
    overrides: dict[str, dict[str, Any]],
) -> list[OverrideDrift]:
    findings: list[OverrideDrift] = []
    for key, override_entry in overrides.items():
        base_entry = base.get(key)
        if base_entry is None:
            findings.append(
                OverrideDrift(key, "gap_fill", "not in LiteLLM; override is the only source")
            )
            continue
        pinned = {f: override_entry[f] for f in _PRICE_FIELDS if f in override_entry}
        deltas = {f: (base_entry.get(f), v) for f, v in pinned.items() if base_entry.get(f) != v}
        if deltas:
            detail = ", ".join(
                f"{f}: LiteLLM={b!r} vs override={o!r}" for f, (b, o) in deltas.items()
            )
            findings.append(OverrideDrift(key, "diverged", detail))
        elif not pinned:
            findings.append(
                OverrideDrift(key, "redundant", "pins no price field; nothing to override")
            )
        else:
            findings.append(
                OverrideDrift(
                    key, "redundant", "all pinned price fields match LiteLLM; safe to remove"
                )
            )
    return findings


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
