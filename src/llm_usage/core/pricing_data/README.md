# Pricing data

`prices.json` is a vendored, trimmed copy of LiteLLM's
[`model_prices_and_context_window_backup.json`](https://raw.githubusercontent.com/BerriAI/litellm/main/litellm/model_prices_and_context_window_backup.json)
restricted to the v1 providers and to LLM-style modes.

## Filter criteria

- `litellm_provider` ∈ `{anthropic, openai, deepseek, dashscope}`
  - `anthropic` — direct Anthropic API
  - `openai` — direct OpenAI API
  - `deepseek` — DeepSeek
  - `dashscope` — Alibaba's DashScope endpoint, the API behind Qwen
- `mode` ∈ `{chat, responses}` — chat completions and OpenAI's Responses API. Embeddings, audio, image, moderation, video are excluded; pricing for those doesn't fit a per-million-token shape.

## Schema

LiteLLM's shape, kept verbatim — same field names, same units (per-token, not per-million-token).

```jsonc
{
  "claude-sonnet-4-5": {
    "litellm_provider": "anthropic",
    "mode": "chat",
    "input_cost_per_token": 0.000003,                   // → input_per_million_usd = 3.00
    "output_cost_per_token": 0.000015,                  // → output_per_million_usd = 15.00
    "cache_creation_input_token_cost": 0.00000375,      // → cache_write_per_million_usd = 3.75
    "cache_read_input_token_cost": 3E-7,                // → cache_read_per_million_usd = 0.30
    "max_input_tokens": 200000,
    "supports_prompt_caching": true,
    // … other capability flags retained for future use
  }
}
```

Provider quirks the loader will need to handle:

- **Anthropic** — has `cache_creation_input_token_cost` *and* `cache_read_input_token_cost`. Maps directly to our `cache_write_per_million_usd` / `cache_read_per_million_usd`.
- **OpenAI / DeepSeek** — only `cache_read_input_token_cost`. They absorb the cache-write cost into input. `cache_write_per_million_usd` should be set to `None` in `pricing_snapshot`.
- **Tiered pricing** — Anthropic and others have `*_above_200k_tokens` variants. v1 stores only the base rate; tiered handling is a future enhancement.

## Refresh procedure

Run the refresh script — it downloads LiteLLM's JSON, applies the
filter below, and rewrites this file:

```bash
bash scripts/refresh_pricing.sh
```

The script is idempotent: with no upstream change it leaves the file
byte-identical. It is also what the weekly **Refresh pricing data**
GitHub Action (`.github/workflows/refresh-pricing.yml`) runs — that
action opens a PR whenever the refresh produces a diff.

The filter the script applies:

- `litellm_provider` ∈ `{anthropic, openai, deepseek, dashscope}`
- `mode` ∈ `{chat, responses}`
- the entry carries rates (`input_cost_per_token` or `tiered_pricing`)
  — this excludes a couple of metadata-only entries
  (`dashscope/qwen3-30b-a3b`, `openai/container`) that LiteLLM keeps
  but provides no prices for; the loader couldn't price them anyway.

Output is written through `jq -S`, so **every key is sorted** — the
top-level model names and each entry's fields. This keeps the
committed file deterministic: a refresh diff only ever shows real
price / model changes, never an ordering reshuffle.

## Why vendor LiteLLM's shape verbatim instead of converting

Refresh stays a one-liner: download → filter → commit. Any conversion to our `pricing_snapshot` shape happens once at load time in the (forthcoming) loader; the committed JSON stays compatible with anyone else who parses LiteLLM data.

## Overrides — `pricing_overrides.json`

`pricing_overrides.json` is a small, manually-maintained sibling of `prices.json` for models / rates LiteLLM is missing or wrong about. The loader **field-merges** it on top of the LiteLLM snapshot before parsing:

- A key only in `pricing_overrides.json` → **adds a new model** (use case: a model LiteLLM hasn't catalogued yet, like `deepseek-v4-flash` was on 2026-05-25).
- A key present in both → **field-level merge**, override fields win. An override that only sets `input_cost_per_token` leaves every other field (`output_cost_per_token`, `cache_read_input_token_cost`, `litellm_provider`, `mode`, …) from the base entry intact.
- A key only in `prices.json` → **unchanged** (the normal path for the ~177 LiteLLM-tracked models).

The merge happens in `pricing_loader._merge_overrides`. Schema mirrors LiteLLM's exactly — same field names, same per-token units. JSON only (no comments allowed).

### When to add an override

Sparing. Three legitimate cases:

1. **LiteLLM catalog lag.** A provider ships a new model (or aliases an old name to a new backend) before LiteLLM catalogues it. Symptom: a real call records `cost_nano_usd = 0` with a "model not in pricing table" warning. Today's example: `deepseek-v4-flash` — the model `deepseek-chat` auto-routes to.
2. **Regional rate divergence.** LiteLLM tracks one schedule per model (typically International / English-docs prices). If you operate in a different region — China-mainland Bailian rates for Qwen, for instance — your real bill diverges from what we record. An override pins the rates that match your bill.
3. **Pricing data quality issues.** Rare. LiteLLM is usually right, but if a rate is demonstrably wrong, an override patches it locally while you upstream the fix.

### When NOT to add an override

- **As a general pricing source.** `prices.json` is auto-refreshed weekly from LiteLLM; overrides are *not*. An entry you copy in today and forget will go stale when the provider next changes their rates.
- **For a model already in `prices.json`.** Trust LiteLLM unless you have evidence they're wrong.
- **Before trying to upstream.** If LiteLLM is missing a model, file an issue / PR at https://github.com/BerriAI/litellm/issues — fixes there benefit everyone. Use an override only as the local stopgap until the upstream fix lands.

### How to add one

1. Look up the model's current rates on the provider's pricing page. Use their **per-token** (USD) numbers — same shape as LiteLLM.
2. Add an entry keyed by `<provider>/<model>` (or just `<model>`, both work; the loader strips the prefix).
3. Include at minimum: `litellm_provider`, `mode`, `input_cost_per_token`, `output_cost_per_token`. Cache fields if applicable. A `source` URL — your future self will thank you when checking what's stale.
4. Run the test suite. The override should round-trip through `load_vendored_pricing` cleanly and the new model should be queryable via `get_pricing`.

### Survives the weekly refresh

`scripts/refresh_pricing.sh` only writes `prices.json`. `pricing_overrides.json` is untouched on refresh — that's the whole point of keeping the two files separate.

### Drift detection (keeps overrides from going stale)

Because overrides win over LiteLLM *unconditionally*, a forgotten one silently masks correct upstream prices — e.g. a stale `deepseek-v4-pro` pin 4x-overcharged until it was caught. `detect_override_drift()` (exercised by `tests/test_pricing_drift.py`, so it runs in CI) compares every override against the raw LiteLLM catalog and classifies it:

- **redundant** — the pinned price now matches LiteLLM. CI **fails**: delete the entry, LiteLLM has it covered.
- **diverged** — the pin differs from LiteLLM. CI **passes** but emits a warning with the delta, so a reviewer re-confirms the pin is still intentional during the next pricing-refresh PR.
- **gap_fill** — LiteLLM doesn't carry the model; the override is the only source. Fine.

Document *why* each override exists with optional `_reason` and `_added` keys — the loader and drift check ignore any `_`-prefixed field:

```jsonc
"deepseek/some-model": {
  "litellm_provider": "deepseek",
  "input_cost_per_token": 1.0e-6,
  "output_cost_per_token": 2.0e-6,
  "_reason": "LiteLLM still lists the launch-promo rate",
  "_added": "2026-06-23"
}
```
