# Backlog

Items planned but not currently in flight. Each entry: trigger (when to act), state, and the artifact text ready to use.

---

## Upstream contribution: LiteLLM `deepseek-v4-flash` / `deepseek-v4-pro`

**State:** drafted, ready to file. Filing waits on the user.

**Why this matters.** DeepSeek aliases the public `deepseek-chat` model name to whichever production backend is current. As of 2026-05-25 they point it at `deepseek-v4-flash`. LiteLLM doesn't catalog `deepseek-v4-flash` or `deepseek-v4-pro` yet, so anyone computing cost from `response.model` — including this project before PR #35 — gets zero. We patched locally via `pricing_overrides.json` (PR #35); the right fix is upstream because every LiteLLM consumer benefits and we can eventually drop the local override.

**Trigger.** File when you have ~30 minutes and an open LiteLLM workspace. No rush — the local override is the failsafe.

**Rate-card note.** `deepseek-v4-pro` had a launch promo that ends 2026-05-31. The rates below are post-promo (effective 2026-06-01), which is what PR #35's override file also locked in. If you file the issue/PR today, flag in the body that the v4-pro rates kick in tomorrow.

---

### Issue draft

File at: <https://github.com/BerriAI/litellm/issues>

**Title:** Add pricing for DeepSeek `deepseek-v4-flash` and `deepseek-v4-pro` (currently missing; `deepseek-chat` aliases to v4-flash in production)

**Body:**

```markdown
## Summary

`model_prices_and_context_window_backup.json` is missing entries for two
DeepSeek models that are live in production today:

- `deepseek-v4-flash` — what the public `deepseek-chat` alias resolves
  to in DeepSeek's current production routing. Verified 2026-05-25:
  `POST /chat/completions` with `model=deepseek-chat` returns
  `response.model = "deepseek-v4-flash"`.
- `deepseek-v4-pro` — the higher-tier model in the same family.

Because LiteLLM doesn't have these entries, downstream consumers
computing cost from `response.model` fall back to zero / unknown.

## Evidence

Source: https://api-docs.deepseek.com/quick_start/pricing
(USD pricing tab; international rates, not mainland-China Bailian.)

Current rates (USD per million tokens, post-2026-05-31 — v4-pro's
launch promo ends 2026-05-31):

| model              | input | output | cache hit | cache miss |
|--------------------|-------|--------|-----------|------------|
| deepseek-v4-flash  | 0.14  | 0.28   | 0.0028    | 0.14       |
| deepseek-v4-pro    | 1.74  | 3.48   | 0.0145    | 1.74       |

Cache-miss = full input price; cache-write cost = 0 in DeepSeek's
model (they absorb it into input — same convention LiteLLM already
uses for OpenAI / existing DeepSeek entries).

## Confirmed via real calls

Tested 2026-05-25 with a 10-input / 1-output call:

- Request: `model=deepseek-chat`
- Response: `model=deepseek-v4-flash`
- Expected cost: `10 * 0.14e-6 + 1 * 0.28e-6 = 1.68e-6 USD` = 1,680 nano-USD
- Matches the dashboard billing for that request.

## Why this matters

Any LiteLLM consumer (or downstream tool using this pricing JSON) computing
DeepSeek spend gets `cost = 0` today, silently, because the model the
API returns isn't in the catalog. Local workarounds exist; an upstream
fix benefits every consumer.

PR to follow.
```

---

### PR draft

File at: <https://github.com/BerriAI/litellm/pulls>

**Title:** Add pricing for `deepseek-v4-flash` and `deepseek-v4-pro`

**Body:**

```markdown
Adds two DeepSeek model entries to `model_prices_and_context_window_backup.json`.

Closes #<issue number from the linked issue>.

## Why

`deepseek-chat` (the public model alias) currently routes to
`deepseek-v4-flash` in DeepSeek's production. Consumers computing
cost from `response.model` can't find either v4-flash or v4-pro
in the JSON, so cost falls back to zero.

## Rates

From <https://api-docs.deepseek.com/quick_start/pricing> (USD tab,
post-2026-05-31 — `deepseek-v4-pro`'s launch promo ends 2026-05-31):

- `deepseek-v4-flash`: $0.14/M input, $0.28/M output, $0.0028/M cache hit
- `deepseek-v4-pro`:   $1.74/M input, $3.48/M output, $0.0145/M cache hit

DeepSeek's pricing model absorbs cache-write into input — same as the
existing OpenAI / DeepSeek entries here — so `cache_creation_input_token_cost`
is `0`, only `cache_read_input_token_cost` is populated.

## Test plan

- Verified rates via a real billed call: `model=deepseek-chat`,
  10-input / 1-output tokens → `response.model = deepseek-v4-flash` →
  expected cost = `10 * 0.14e-6 + 1 * 0.28e-6 = 1.68e-6 USD`. Matches
  the per-call billing shown in the DeepSeek dashboard.
- JSON validates: `jq . model_prices_and_context_window_backup.json > /dev/null`.
```

**JSON to add.** Insert sorted alphabetically to match the file's `jq -S` convention:

```jsonc
"deepseek/deepseek-v4-flash": {
  "cache_creation_input_token_cost": 0.0,
  "cache_read_input_token_cost": 2.8e-9,
  "input_cost_per_token": 1.4e-7,
  "litellm_provider": "deepseek",
  "max_input_tokens": 1000000,
  "max_output_tokens": 384000,
  "mode": "chat",
  "output_cost_per_token": 2.8e-7,
  "source": "https://api-docs.deepseek.com/quick_start/pricing",
  "supports_function_calling": true,
  "supports_tool_choice": true
},
"deepseek/deepseek-v4-pro": {
  "cache_creation_input_token_cost": 0.0,
  "cache_read_input_token_cost": 1.45e-8,
  "input_cost_per_token": 1.74e-6,
  "litellm_provider": "deepseek",
  "max_input_tokens": 1000000,
  "max_output_tokens": 384000,
  "mode": "chat",
  "output_cost_per_token": 3.48e-6,
  "source": "https://api-docs.deepseek.com/quick_start/pricing",
  "supports_function_calling": true,
  "supports_tool_choice": true
}
```

---

### After it lands

Once LiteLLM merges this, the next `scripts/refresh_pricing.sh` run pulls these entries into `prices.json` automatically. At that point:

- The override entries in `src/llm_usage/core/pricing_data/pricing_overrides.json` can be removed — the loader's field-merge means leaving them is harmless, but they're no longer load-bearing.
- Or keep them as a pin against future regressions / upstream rate changes; the field-merge does the right thing either way.

Pick based on whether you want the entries locked at the values you tested with (keep override) or always tracking LiteLLM's current numbers (remove override).
