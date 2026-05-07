# Post-v1 Provider Roadmap

This doc estimates the work to extend `llm-usage-mcp` beyond its v1 provider scope toward the broader vision.

- **v1** captures: Anthropic, OpenAI, Qwen (via DashScope), DeepSeek.
- **Vision adds**: Google (Gemini, direct + via Vertex), AWS Bedrock (all model families), Moonshot, plus an open door for other Chinese providers (Zhipu/GLM, MiniMax, Baidu/ERNIE, ByteDance/Doubao, Tencent/Hunyuan).

Estimates are rough person-days for a developer who knows this codebase and the target API. Treat ranges, not point estimates, as the contract.

---

## The two costs aren't the same

Every provider expansion has two independent budgets — bundling them is the easiest way to misjudge effort.

| Cost | What it covers | Per-provider effort |
|---|---|---|
| **Pricing data** | Adding the provider's models to `prices.json` and the loader that maps to `pricing_snapshot`. | Hours, not days. Mostly a `jq`-filter change. |
| **Capture adapter** | Automatic recording from a live API call: parse the API's usage object, normalize cache fields to our schema, hook into a proxy or SDK wrapper. | Days to weeks. Each new API shape is real engineering. |

A provider can be **half-supported** indefinitely: pricing in the table, adapter not built. Users call `record_usage` manually with token counts they extract themselves. This is a legitimate state — useful for low-volume providers where the adapter ROI doesn't justify the work.

The estimates below are for the **adapter** (the expensive cost). Pricing-data work is the cheap part of fulfilling the vision and can land in a single ~2-hour PR whenever we want.

---

## Per-provider estimates

### Google Gemini (direct via AI Studio)

- **Path**: B (SDK wrapper). The Path A OpenAI proxy doesn't speak Gemini's wire format.
- **API surface**: `/v1beta/models/{model}:generateContent` and `:streamGenerateContent`. SDK: `google-genai`.
- **Usage object**: `usageMetadata: { promptTokenCount, candidatesTokenCount, totalTokenCount, cachedContentTokenCount }`.
- **Streaming**: SSE; each chunk includes incremental usage. Different from OpenAI's "usage on final chunk" pattern — needs accumulation logic.
- **Cache**: Gemini Context Caching has explicit cache creation (separate API call) and cache reads (passed via `cachedContent`). Pricing differs by model. Maps cleanly to our `cache_write_tokens` / `cache_read_tokens` once we know which call is which.
- **Auth**: API key (simple).
- **Effort**: **3–5 days**. Includes new SSE parser, `usageMetadata` normalizer, fixtures, tests, end-to-end record check against a real Gemini call.

**Risks / unknowns**

- Vertex AI variant of Gemini has a different wire format (`generateContent` request shape differs subtly). v1.x scope = direct AI Studio only; Vertex Gemini is a separate adapter.
- Gemini's `cachedContentTokenCount` does not always equal what's billed at the cache-read rate; need to verify against the billing dashboard.

### AWS Bedrock

- **Path**: B (SDK wrapper). Path A is theoretically possible but SigV4 signing in a proxy is painful — not worth it.
- **API surface**: Two paths.
  - `Converse` API (newer, unified across model families) — strongly preferred.
  - `InvokeModel` (legacy, family-specific bodies) — only worth supporting if a user is already wired up to it.
- **Usage object** (Converse): `usage: { inputTokens, outputTokens, totalTokens, cacheReadInputTokens?, cacheWriteInputTokens? }` (camelCase).
- **Streaming**: Bedrock's `ConverseStream` returns event-stream chunks; final event carries usage. Different framing from OpenAI/Anthropic SSE — closer to Bedrock's binary frame protocol via `boto3` event iterator. Not standard SSE.
- **Cache**: prompt caching is generally available on Claude, Nova; field names map directly via Converse. For `InvokeModel`, each model family has its own usage shape — Claude returns Anthropic's native object, Llama returns its own.
- **Auth**: AWS SigV4 (access key + secret + region). Affects both adapter and any future proxy plans.
- **Pricing data**: Bedrock pricing is per-region in some cases. Our `pricing_snapshot` schema doesn't carry a region column today. Either store the most expensive region (US-East default) or extend the schema. Decide before adapter work starts.
- **Effort**: **5–8 days** for Converse-only. Add ~3 days for `InvokeModel` per family if needed.

**Risks / unknowns**

- Bedrock pricing is more granular than the JSON we vendor; LiteLLM's data may not capture per-region pricing. Budget for ~1 day of pricing-data plumbing.
- `boto3` event iterator parsing is bespoke per service; no shared abstraction with OpenAI/Anthropic SSE.
- Some Bedrock models (Stable Diffusion, embedding models) have non-token billing — already excluded from our `chat`/`responses` mode filter, so safe to ignore for now.

### Moonshot (Kimi)

- **Path**: A (proxy). Moonshot exposes an OpenAI-compatible chat completions endpoint.
- **API surface**: `https://api.moonshot.cn/v1/chat/completions` — same shape as OpenAI.
- **Usage object**: same as OpenAI (`prompt_tokens`, `completion_tokens`, `total_tokens`, sometimes `cached_tokens`).
- **Streaming**: same as OpenAI (`stream_options.include_usage=true` for usage on the final chunk).
- **Cache**: Moonshot's context caching follows OpenAI's pattern (cached portion of input, no separate write event).
- **Auth**: API key (Bearer header).
- **Effort**: **1–2 days**. Mostly configuration — point the existing OpenAI adapter at `api.moonshot.cn`, verify token counts against Moonshot's dashboard, add fixtures.

**Risks / unknowns**

- Moonshot's `cached_tokens` field naming may drift over time; verify field names against current docs at adapter time.

### Other Chinese providers (Zhipu, MiniMax, Doubao, etc.)

- **Path**: A (proxy) for OpenAI-compatible endpoints. Most fall here.
- **Pattern**: Once Moonshot ships, each subsequent OpenAI-compatible Chinese provider is a **half-day** of config + fixtures, assuming no surprises.
- **Outliers**: Baidu (ERNIE) and Tencent (Hunyuan) have proprietary APIs — closer to Path B and ~2–3 days each.
- **Currency**: Chinese providers price in CNY. The pricing loader needs an FX-conversion step for `pricing_snapshot` (which stores USD). v1 already has DashScope/DeepSeek as CNY-priced — when their pricing ingestion lands, it solves the FX problem for everyone else too.

---

## Cross-cutting work that benefits multiple adapters

Some effort is shared. Doing these once unlocks several providers more cheaply.

| Task | Beneficiaries | Estimate |
|---|---|---|
| Tiered-pricing handler in loader (already noted in `prices.json` docs) | Qwen, future tiered providers | 0.5–1 day |
| FX conversion (CNY → USD) at load time | All Chinese providers | 1 day, plus ongoing rate refresh story |
| Streaming SSE parser abstraction (OpenAI / Anthropic / Gemini variants) | Gemini, future SSE providers | 2 days |
| Bedrock event-stream parser | Bedrock only (no shared lift) | included in Bedrock estimate |
| Per-region pricing column in `pricing_snapshot` | Bedrock, possibly Vertex | 1 day for schema + migration |
| AWS SigV4 helper module | Bedrock, future AWS-hosted providers | 0.5 day if `boto3` does it for us, ~2 days otherwise |

---

## Suggested phasing

Each step assumes the previous merged. Adjust based on which providers you actually use.

| Step | Scope | Estimated effort | Why this order |
|---|---|---|---|
| **v1.1** | Broaden `prices.json` to all vision providers (data only). | ~0.5 day | Cheap, unblocks manual `record_usage` for any vision provider immediately. |
| **v1.2** | Moonshot adapter (Path A, OpenAI-compatible). | 1–2 days | Lowest-risk new adapter; validates the "swap base URL" pattern. |
| **v1.3** | Google Gemini direct (Path B). | 3–5 days | First non-OpenAI-shape adapter; forces the SSE-parser abstraction. |
| **v1.4** | AWS Bedrock Converse (Path B). | 5–8 days + 1 day pricing | Most complex; benefits from the SSE abstraction landing first. |
| **v1.5** | One additional Chinese provider (e.g., Zhipu/GLM). | 0.5–1 day | Validates the "OpenAI-compatible-ish-but-not-quite" pattern for the family. |
| **v1.6+** | Remaining providers as users actually need them. | Variable | Stop guessing demand; let usage drive priority. |

Total to reach the full vision (excluding v1.6+): **~14–20 days** of focused work, plus pricing-data PRs which can land in parallel.

---

## Decisions to make before starting any of this

These aren't blockers for v1 but will shape the post-v1 PRs. Better to settle the answers in advance.

1. **Region in `pricing_snapshot`**. Bedrock prices vary by region. Add a `region` column with a sentinel (`""` for non-region pricing) or pick a single canonical region per model? Affects schema and migration.
2. **CNY → USD conversion**. Static FX rate vendored alongside `prices.json`, or live lookup at load time? Static is simpler; live needs an FX service dependency.
3. **`InvokeModel` vs `Converse` for Bedrock**. v1.4 ships Converse only. Anyone needing `InvokeModel` does the per-family adapter work themselves — or do we promise both?
4. **Vertex Gemini scope**. Treat as a separate provider (different `litellm_provider` value) or a configuration of the direct Gemini adapter? Affects whether Vertex needs its own pricing rows.
5. **Cache pricing missing for non-Anthropic providers**. OpenAI/DeepSeek/Moonshot absorb cache-write into input. Our schema's `cache_write_per_million_usd` is `None` for them. The loader needs explicit handling — either set it null and let `CostCalculator` skip, or set it equal to input rate and let the math be uniform. Pick one.

Each is a 30-minute conversation; none should block scheduling the work above.
