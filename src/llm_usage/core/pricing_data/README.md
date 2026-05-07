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

Manual for now (a weekly GitHub Action is on the roadmap):

```bash
curl -fsSL -o /tmp/litellm.json \
  https://raw.githubusercontent.com/BerriAI/litellm/main/litellm/model_prices_and_context_window_backup.json

jq '
  to_entries
  | map(select(
      (.value.litellm_provider == "anthropic" or
       .value.litellm_provider == "openai" or
       .value.litellm_provider == "deepseek" or
       .value.litellm_provider == "dashscope")
      and (.value.mode == "chat" or .value.mode == "responses")
      and (.value | has("input_cost_per_token") or has("tiered_pricing"))
    ))
  | from_entries
' /tmp/litellm.json > src/llm_usage/core/pricing_data/prices.json
```

The final clause excludes a couple of entries (`dashscope/qwen3-30b-a3b`, `openai/container`) that LiteLLM keeps for metadata but provides no rates for — the loader couldn't price them anyway.

## Why vendor LiteLLM's shape verbatim instead of converting

Refresh stays a one-liner: download → filter → commit. Any conversion to our `pricing_snapshot` shape happens once at load time in the (forthcoming) loader; the committed JSON stays compatible with anyone else who parses LiteLLM data.
