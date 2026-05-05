# Provider Adapter Reference — v1 Four Providers

Concrete request/response shapes for **Anthropic, OpenAI, Qwen (DashScope), DeepSeek** — the four providers Project A supports in v1. Use these as test fixtures and as the spec your `Provider` adapter implementations must conform to.

**Reminder**: every sample below is illustrative — the field structure matches each provider's documented schema, but you should always verify against the provider's official API docs and capture a *real* sample response into `tests/fixtures/sample_responses/{provider}.json` before relying on the parser.

---

## Common Adapter Interface

Every provider adapter you write must produce a normalized `UsageEvent`:

```python
@dataclass
class UsageEvent:
    provider: str                 # "anthropic" | "openai" | "qwen" | "deepseek"
    model: str
    input_tokens: int
    output_tokens: int
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0
    duration_ms: int | None = None
    request_id: str | None = None      # for idempotency
    raw_response: dict | None = None   # debugging only
```

The adapter's job is just: **take the raw HTTP response, return the `UsageEvent`**. Cost is computed downstream from pricing data.

---

## 1. Anthropic — `api.anthropic.com/v1/messages`

### Endpoint

```
POST https://api.anthropic.com/v1/messages
```

### Auth headers

```
x-api-key:           $ANTHROPIC_API_KEY
anthropic-version:   2023-06-01
content-type:        application/json
```

### Sample request body

```json
{
  "model": "claude-sonnet-4-6",
  "max_tokens": 1024,
  "messages": [
    {"role": "user", "content": "What is the capital of France?"}
  ]
}
```

### Sample non-streaming response

```json
{
  "id": "msg_01ABCxyz",
  "type": "message",
  "role": "assistant",
  "model": "claude-sonnet-4-6",
  "content": [
    {"type": "text", "text": "The capital of France is Paris."}
  ],
  "stop_reason": "end_turn",
  "stop_sequence": null,
  "usage": {
    "input_tokens": 13,
    "output_tokens": 9,
    "cache_creation_input_tokens": 0,
    "cache_read_input_tokens": 0
  }
}
```

### Token field paths

| Field | JSON path | Notes |
|---|---|---|
| Input tokens | `usage.input_tokens` | |
| Output tokens | `usage.output_tokens` | |
| Cache write tokens | `usage.cache_creation_input_tokens` | Anthropic-only; counts tokens written to cache |
| Cache read tokens | `usage.cache_read_input_tokens` | Anthropic-only; counts tokens read from cache |
| Request ID | `id` (top-level), e.g. `msg_01ABCxyz` | Use as the idempotency key |
| Model echo | `model` | Trust this over the request — Anthropic may auto-route |

### Streaming — Server-Sent Events

When `stream: true` is in the request, Anthropic returns SSE events. Usage data is split across **two events** — you must accumulate both.

```
event: message_start
data: {"type":"message_start","message":{"id":"msg_01ABC","type":"message","role":"assistant","content":[],"model":"claude-sonnet-4-6","stop_reason":null,"stop_sequence":null,"usage":{"input_tokens":13,"output_tokens":1,"cache_creation_input_tokens":0,"cache_read_input_tokens":0}}}

event: content_block_start
data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}

event: content_block_delta
data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"The"}}

event: content_block_delta
data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":" capital..."}}

event: content_block_stop
data: {"type":"content_block_stop","index":0}

event: message_delta
data: {"type":"message_delta","delta":{"stop_reason":"end_turn","stop_sequence":null},"usage":{"output_tokens":9}}

event: message_stop
data: {"type":"message_stop"}
```

**Critical parsing rule**: 
- `message_start.message.usage` has `input_tokens`, `cache_creation_input_tokens`, `cache_read_input_tokens` and an *initial* `output_tokens` (often 1)
- `message_delta.usage.output_tokens` has the *final* cumulative output token count
- Use the `message_start` values for input + cache fields, and the `message_delta` value for output

### Cost calculation

Anthropic has a **four-component pricing** for cached inputs:

| Token type | Multiplier vs base input price |
|---|---|
| Standard input | 1.0× |
| Cache write (creation) | **1.25×** |
| Cache read | **0.1×** |
| Output | (separate output price, typically 5× input) |

Example for `claude-sonnet-4-6` (illustrative pricing — verify against current `anthropic.com/pricing`):

```
input  : $3.00 / 1M tokens
output : $15.00 / 1M tokens
cache_write : $3.75 / 1M tokens   (= input × 1.25)
cache_read  : $0.30 / 1M tokens   (= input × 0.1)
```

Cost formula:

```
cost_usd = (input_tokens         * input_price)
         + (output_tokens        * output_price)
         + (cache_write_tokens   * cache_write_price)
         + (cache_read_tokens    * cache_read_price)
```

**Note**: in Anthropic's accounting, `input_tokens` already excludes cache_read tokens — they're billed separately. Don't double-count.

### Pricing JSON entry format

```json
"anthropic/claude-sonnet-4-6": {
  "provider": "anthropic",
  "input_per_million_usd": 3.00,
  "output_per_million_usd": 15.00,
  "cache_write_per_million_usd": 3.75,
  "cache_read_per_million_usd": 0.30,
  "context_window": 200000,
  "good_for_code": true,
  "good_for_reasoning": true,
  "quality_tier": 1
}
```

### Quirks & gotchas

- **Cache fields can be `null` or absent** in older API versions — treat missing as 0
- **`model` echo may differ from request** if you use a model alias; record what comes back, not what you sent
- **Tool use responses** include input_tokens that count the tool definitions; budget for this if comparing across providers
- **Vision (image) tokens** are billed as input tokens; the count is computed by Anthropic and returned in `input_tokens` — no separate field

---

## 2. OpenAI — `api.openai.com/v1/chat/completions`

### Endpoint

```
POST https://api.openai.com/v1/chat/completions
```

### Auth header

```
Authorization: Bearer $OPENAI_API_KEY
content-type:  application/json
```

### Sample request body

```json
{
  "model": "gpt-5.2",
  "messages": [
    {"role": "user", "content": "What is the capital of France?"}
  ]
}
```

### Sample non-streaming response

```json
{
  "id": "chatcmpl-9xyz123",
  "object": "chat.completion",
  "created": 1714829400,
  "model": "gpt-5.2",
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "content": "The capital of France is Paris."
      },
      "finish_reason": "stop"
    }
  ],
  "usage": {
    "prompt_tokens": 13,
    "completion_tokens": 9,
    "total_tokens": 22,
    "prompt_tokens_details": {
      "cached_tokens": 0
    }
  }
}
```

### Token field paths

| Field | JSON path | Notes |
|---|---|---|
| Input tokens (total) | `usage.prompt_tokens` | Includes cached portion |
| Output tokens | `usage.completion_tokens` | |
| Cache read tokens | `usage.prompt_tokens_details.cached_tokens` | OpenAI-style; subtract from `prompt_tokens` to get *uncached* input |
| Reasoning tokens (o-series, GPT-5.2) | `usage.completion_tokens_details.reasoning_tokens` | Counted within completion_tokens but priced as output |
| Request ID | `id` | Use for idempotency |
| Model | `model` | Trust the response |

**OpenAI's accounting differs from Anthropic's**: `prompt_tokens` is the **total** input (cached + uncached). To get uncached:

```python
uncached_input = prompt_tokens - prompt_tokens_details.cached_tokens
cached_read    = prompt_tokens_details.cached_tokens
```

### Streaming — must opt in for usage

By default, **streaming responses do NOT include usage**. You must set:

```json
{
  "model": "gpt-5.2",
  "messages": [...],
  "stream": true,
  "stream_options": {"include_usage": true}
}
```

The final SSE chunk will then have `choices: []` and a populated `usage` field:

```
data: {"id":"chatcmpl-9xyz","object":"chat.completion.chunk","created":1714829400,"model":"gpt-5.2","choices":[{"index":0,"delta":{"role":"assistant","content":""},"finish_reason":null}]}

data: {"id":"chatcmpl-9xyz","object":"chat.completion.chunk","created":1714829400,"model":"gpt-5.2","choices":[{"index":0,"delta":{"content":"The"},"finish_reason":null}]}

data: {"id":"chatcmpl-9xyz","object":"chat.completion.chunk","created":1714829400,"model":"gpt-5.2","choices":[{"index":0,"delta":{"content":" capital..."},"finish_reason":null}]}

data: {"id":"chatcmpl-9xyz","object":"chat.completion.chunk","created":1714829400,"model":"gpt-5.2","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}

data: {"id":"chatcmpl-9xyz","object":"chat.completion.chunk","created":1714829400,"model":"gpt-5.2","choices":[],"usage":{"prompt_tokens":13,"completion_tokens":9,"total_tokens":22,"prompt_tokens_details":{"cached_tokens":0}}}

data: [DONE]
```

**Critical parsing rule**: the chunk with `choices: []` is the usage chunk. **Without `stream_options.include_usage=true`, you will NOT get usage at all**. Your proxy should inject this option for streaming requests if not present.

### Cost calculation

```
cost_usd = (uncached_input_tokens * input_price)
         + (cached_read_tokens    * cached_input_price)
         + (completion_tokens     * output_price)
```

Where for most models, `cached_input_price ≈ 0.5 × input_price`.

For reasoning models (o-series, GPT-5.2 with reasoning), reasoning tokens are inside `completion_tokens` and billed at the standard output rate — no separate calculation needed.

### Pricing JSON entry format

```json
"openai/gpt-5.2": {
  "provider": "openai",
  "input_per_million_usd": 10.00,
  "output_per_million_usd": 30.00,
  "cache_read_per_million_usd": 5.00,
  "context_window": 400000,
  "good_for_code": true,
  "good_for_reasoning": true,
  "quality_tier": 1
}
```

Note: OpenAI has no "cache write" pricing tier — just cached read at a discount.

### Quirks & gotchas

- **`stream_options.include_usage` is opt-in**. Without it, streaming gives you NO usage data. Document this prominently in your proxy / wrapper.
- **`prompt_tokens` includes cached** — don't double-count. Uncached = total − cached.
- **Reasoning tokens** are billed at output price even though they're "thinking," not "output." Don't need a separate field.
- **`choices` may be missing or empty** in the usage chunk; your parser should look for `usage` regardless of `choices` state.
- **Some Azure OpenAI deployments** return slightly different shapes — out of scope for v1.

---

## 3. Qwen / Alibaba DashScope — OpenAI-Compatible Mode

### Endpoint

```
POST https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions
```

This is the **OpenAI-compatible endpoint**. DashScope has its own native API too, but for v1 use the compatible endpoint — your OpenAI adapter mostly just works.

### Auth header

```
Authorization: Bearer $DASHSCOPE_API_KEY
content-type:  application/json
```

### Sample request body

```json
{
  "model": "qwen-max",
  "messages": [
    {"role": "user", "content": "What is the capital of France?"}
  ]
}
```

### Sample non-streaming response

```json
{
  "id": "chatcmpl-abc123def456",
  "object": "chat.completion",
  "created": 1714829400,
  "model": "qwen-max",
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "content": "The capital of France is Paris."
      },
      "finish_reason": "stop"
    }
  ],
  "usage": {
    "prompt_tokens": 14,
    "completion_tokens": 8,
    "total_tokens": 22
  }
}
```

### Token field paths

Same as OpenAI for the basics:

| Field | JSON path | Notes |
|---|---|---|
| Input tokens | `usage.prompt_tokens` | |
| Output tokens | `usage.completion_tokens` | |
| Cache fields | *not always present* | DashScope's compatible mode may omit cache token detail |

### Streaming

Same as OpenAI — SSE chunks. Set `stream: true` and include `stream_options.include_usage: true` if supported by the specific Qwen model. Some model variants on DashScope may not honor `stream_options`; if missing, fall back to **not capturing streaming usage** for that model and document the limitation.

### Cost calculation — currency conversion needed

DashScope prices are in **CNY (¥)** on their pricing page. Your pricing JSON should:

1. Store the original CNY price
2. Convert to USD at refresh time using a fixed FX rate
3. Document the FX rate and refresh date in pricing JSON metadata

Example pricing JSON entry:

```json
"qwen/qwen-max": {
  "provider": "qwen",
  "input_per_million_usd": 2.80,
  "output_per_million_usd": 8.40,
  "input_per_million_cny": 20.00,
  "output_per_million_cny": 60.00,
  "fx_rate_cny_to_usd": 0.14,
  "fx_rate_fetched_at": "2026-05-04",
  "context_window": 32768,
  "good_for_code": true,
  "good_for_reasoning": false,
  "quality_tier": 2
}
```

Cost formula is OpenAI-shaped:

```
cost_usd = (prompt_tokens     * input_price_usd)
         + (completion_tokens * output_price_usd)
```

### Quirks & gotchas

- **Models from DashScope can include**: `qwen-max`, `qwen-plus`, `qwen-turbo`, `qwen-long`, `qwen-vl-max`, `qwen-coder-plus`, etc. Pricing differs per model — get the full list from DashScope's pricing page.
- **CNY → USD FX volatility**: refresh weekly via your pricing-update GitHub Action.
- **Latency from outside China** can be high for the dashscope.aliyuncs.com endpoint; document this for non-China users.
- **Some Qwen models support a longer-context "long" variant** at different pricing — treat as separate pricing entries.
- **The native DashScope API** has more usage detail (`output_tokens_details.thoughts_count` for thinking models) — out of scope for v1; revisit when you add a native adapter.

---

## 4. DeepSeek — `api.deepseek.com`

### Endpoint

```
POST https://api.deepseek.com/chat/completions
```

(Also accessible at `https://api.deepseek.com/v1/chat/completions` — both work.)

### Auth header

```
Authorization: Bearer $DEEPSEEK_API_KEY
content-type:  application/json
```

### Sample request body

```json
{
  "model": "deepseek-chat",
  "messages": [
    {"role": "user", "content": "What is the capital of France?"}
  ]
}
```

### Sample non-streaming response

```json
{
  "id": "chatcmpl-xyz789",
  "object": "chat.completion",
  "created": 1714829400,
  "model": "deepseek-chat",
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "content": "The capital of France is Paris."
      },
      "finish_reason": "stop"
    }
  ],
  "usage": {
    "prompt_tokens": 14,
    "completion_tokens": 8,
    "total_tokens": 22,
    "prompt_cache_hit_tokens": 0,
    "prompt_cache_miss_tokens": 14
  }
}
```

### Token field paths — note the DeepSeek-specific cache fields

| Field | JSON path | Notes |
|---|---|---|
| Total input tokens | `usage.prompt_tokens` | Full count including cached |
| Output tokens | `usage.completion_tokens` | |
| **Cache hit tokens** | `usage.prompt_cache_hit_tokens` | DeepSeek-specific; tokens served from cache (cheaper) |
| **Cache miss tokens** | `usage.prompt_cache_miss_tokens` | DeepSeek-specific; tokens not served from cache |
| Reasoning tokens (deepseek-reasoner) | `usage.completion_tokens_details.reasoning_tokens` | Within completion_tokens; output-priced |

**DeepSeek's cache split is explicit**: `prompt_cache_hit_tokens + prompt_cache_miss_tokens = prompt_tokens`. Map them like:

```python
cache_read_tokens = prompt_cache_hit_tokens
input_tokens     = prompt_cache_miss_tokens   # the "fresh" portion
# cache_write_tokens: DeepSeek doesn't expose; treat as 0
```

### Streaming

OpenAI-compatible. Set `stream: true`. Final chunk pattern matches OpenAI's; supports `stream_options.include_usage`.

### Cost calculation — three tiers

DeepSeek prices have three tiers per model:

| Tier | Field | Typical price (illustrative) |
|---|---|---|
| Cache miss (normal input) | `prompt_cache_miss_tokens` | full price |
| Cache hit (discounted input) | `prompt_cache_hit_tokens` | ~10–25% of full price |
| Output | `completion_tokens` | output price |

Example pricing entry:

```json
"deepseek/deepseek-chat": {
  "provider": "deepseek",
  "input_per_million_usd": 0.27,
  "output_per_million_usd": 1.10,
  "cache_read_per_million_usd": 0.07,
  "context_window": 64000,
  "good_for_code": true,
  "good_for_reasoning": false,
  "quality_tier": 2
}
```

Cost formula:

```
cost_usd = (cache_miss_tokens * input_price)
         + (cache_hit_tokens  * cache_read_price)
         + (completion_tokens * output_price)
```

### Quirks & gotchas

- **`deepseek-chat` vs `deepseek-reasoner`**: same API, different models, different pricing tiers. Reasoner has higher output prices because of long reasoning traces.
- **Off-peak discount**: DeepSeek offers ~50% discount during off-peak hours (UTC). Out of scope for v1; pricing JSON shows standard pricing only. Note this as future work.
- **CNY pricing on docs page**, USD pricing also published. Use the USD figures directly — no FX conversion needed.
- **Cache TTL is short** (a few minutes); cache hits are common during a multi-turn conversation, rare across separate sessions.

---

## Adapter Implementation Skeleton

```python
# src/llm_usage/capture/adapters/base.py

from abc import ABC, abstractmethod
from llm_usage.core.models import UsageEvent

class ProviderAdapter(ABC):
    name: str  # "anthropic", "openai", "qwen", "deepseek"

    @abstractmethod
    def parse_response(self, response_json: dict, request_id: str | None = None) -> UsageEvent:
        """Parse a non-streaming response."""

    @abstractmethod
    def parse_stream_chunks(self, chunks: list[dict]) -> UsageEvent:
        """Parse accumulated streaming chunks into a single UsageEvent."""
```

### Anthropic adapter — sketch

```python
class AnthropicAdapter(ProviderAdapter):
    name = "anthropic"

    def parse_response(self, response_json, request_id=None):
        usage = response_json["usage"]
        return UsageEvent(
            provider="anthropic",
            model=response_json["model"],
            input_tokens=usage["input_tokens"],
            output_tokens=usage["output_tokens"],
            cache_write_tokens=usage.get("cache_creation_input_tokens", 0) or 0,
            cache_read_tokens=usage.get("cache_read_input_tokens", 0) or 0,
            request_id=request_id or response_json.get("id"),
            raw_response=response_json,
        )

    def parse_stream_chunks(self, chunks):
        # Find message_start (has input + cache)
        # Find message_delta (has final output_tokens)
        ms = next(c for c in chunks if c.get("type") == "message_start")
        md = next(c for c in chunks if c.get("type") == "message_delta")
        ms_usage = ms["message"]["usage"]
        return UsageEvent(
            provider="anthropic",
            model=ms["message"]["model"],
            input_tokens=ms_usage["input_tokens"],
            output_tokens=md["usage"]["output_tokens"],
            cache_write_tokens=ms_usage.get("cache_creation_input_tokens", 0) or 0,
            cache_read_tokens=ms_usage.get("cache_read_input_tokens", 0) or 0,
            request_id=ms["message"]["id"],
        )
```

### OpenAI adapter — sketch

```python
class OpenAIAdapter(ProviderAdapter):
    name = "openai"

    def parse_response(self, response_json, request_id=None):
        usage = response_json["usage"]
        cached = usage.get("prompt_tokens_details", {}).get("cached_tokens", 0) or 0
        return UsageEvent(
            provider="openai",
            model=response_json["model"],
            input_tokens=usage["prompt_tokens"] - cached,
            output_tokens=usage["completion_tokens"],
            cache_read_tokens=cached,
            request_id=request_id or response_json.get("id"),
            raw_response=response_json,
        )
    
    def parse_stream_chunks(self, chunks):
        # The usage chunk has choices=[]
        usage_chunk = next(c for c in chunks if c.get("usage"))
        usage = usage_chunk["usage"]
        cached = usage.get("prompt_tokens_details", {}).get("cached_tokens", 0) or 0
        return UsageEvent(
            provider="openai",
            model=usage_chunk["model"],
            input_tokens=usage["prompt_tokens"] - cached,
            output_tokens=usage["completion_tokens"],
            cache_read_tokens=cached,
            request_id=usage_chunk["id"],
        )
```

### Qwen adapter — extend OpenAI

```python
class QwenAdapter(OpenAIAdapter):
    name = "qwen"
    # Inherits everything; just changes the name. Adjust if Qwen returns extra fields.
```

### DeepSeek adapter

```python
class DeepSeekAdapter(ProviderAdapter):
    name = "deepseek"

    def parse_response(self, response_json, request_id=None):
        usage = response_json["usage"]
        cache_hit = usage.get("prompt_cache_hit_tokens", 0) or 0
        cache_miss = usage.get("prompt_cache_miss_tokens", usage["prompt_tokens"] - cache_hit)
        return UsageEvent(
            provider="deepseek",
            model=response_json["model"],
            input_tokens=cache_miss,
            output_tokens=usage["completion_tokens"],
            cache_read_tokens=cache_hit,
            request_id=request_id or response_json.get("id"),
            raw_response=response_json,
        )

    def parse_stream_chunks(self, chunks):
        usage_chunk = next(c for c in chunks if c.get("usage"))
        return self.parse_response(usage_chunk, request_id=usage_chunk.get("id"))
```

---

## Test Fixture Files to Create on Day 2

```
tests/fixtures/sample_responses/
├── anthropic/
│   ├── nonstreaming_basic.json
│   ├── nonstreaming_with_cache.json
│   └── streaming.txt              # raw SSE log
├── openai/
│   ├── nonstreaming_basic.json
│   ├── nonstreaming_with_cache.json
│   ├── streaming_no_usage.txt     # without stream_options
│   └── streaming_with_usage.txt   # with stream_options
├── qwen/
│   ├── nonstreaming_basic.json
│   └── streaming.txt
└── deepseek/
    ├── nonstreaming_basic.json
    ├── nonstreaming_with_cache_hit.json
    └── streaming.txt
```

**Important**: capture *real* responses by making a few real API calls during Day 1–2 of the build. Synthetic fixtures will miss provider-specific quirks.

---

## Provider Verification Checklist

Before considering each provider "done" in v1:

- [ ] Real non-streaming call captured into fixture
- [ ] Real streaming call captured into fixture
- [ ] Adapter parses both fixtures into a `UsageEvent` correctly (unit test)
- [ ] Cost computed from `UsageEvent` matches what the provider's dashboard says (within 1% — rounding differences are OK)
- [ ] Pricing entry added to `prices.json` with all required fields
- [ ] Provider listed in `list_providers` MCP tool output
- [ ] At least one integration test that hits the live API (gated by env var)
- [ ] README "Supported providers" table updated

---

## Adding a Fifth Provider Later (template)

When you're ready to add Moonshot / Zhipu / Bedrock / etc., follow this template:

1. **Capture a real non-streaming response** into `tests/fixtures/sample_responses/{provider}/`
2. **Write the adapter** — usually subclass `OpenAIAdapter` if compatible
3. **Add pricing JSON entries** for the provider's main models
4. **Add an integration test** gated by `${PROVIDER}_API_KEY` env var
5. **Update README** supported-providers table
6. **Update `list_providers` MCP tool**'s underlying registry

**Total time per new provider once the framework is in place**: ~1 hour.

---

## Quick Reference Card (print and pin to wall)

| Provider | Base URL | Auth header | Cache fields |
|---|---|---|---|
| Anthropic | `api.anthropic.com/v1/messages` | `x-api-key` | `cache_creation_input_tokens`, `cache_read_input_tokens` |
| OpenAI | `api.openai.com/v1/chat/completions` | `Authorization: Bearer` | `prompt_tokens_details.cached_tokens` |
| Qwen | `dashscope.aliyuncs.com/compatible-mode/v1/chat/completions` | `Authorization: Bearer` | (limited; varies) |
| DeepSeek | `api.deepseek.com/chat/completions` | `Authorization: Bearer` | `prompt_cache_hit_tokens`, `prompt_cache_miss_tokens` |

| Provider | Streaming usage field location | Need opt-in? |
|---|---|---|
| Anthropic | Split: `message_start` + `message_delta` | No |
| OpenAI | Final chunk with `choices: []` | **Yes** — `stream_options.include_usage: true` |
| Qwen | Final chunk (OpenAI-shaped) | Sometimes — try opt-in |
| DeepSeek | Final chunk (OpenAI-shaped) | Yes — opt-in |
