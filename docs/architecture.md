# Architecture

The product is three layers stacked on one local SQLite, all in a
single Python process (one for the proxy, one for the MCP server —
they share the DB by virtue of pointing at the same file, not by
talking to each other).

```
Layer 3:  src/llm_usage/mcp/        — MCP tools + resources (read)
Layer 2:  src/llm_usage/core/       — SQLite + pricing + cost math
Layer 1:  src/llm_usage/capture/    — Anthropic proxy + (future) SDK wrappers (write)
```

Layer 1 writes `usage_events` rows through `core/recording.py`. Layer
3 reads them back through `core/spend.py` (totals + summaries) and
`core/pricing.py` (pricing snapshots). The MCP layer never writes
through the recorder directly except for `record_usage`, which exists
so a human or agent can log a call manually when the capture layer
isn't in the picture.

## Capture layer: non-streaming vs streaming

The Anthropic `/v1/messages` proxy ships in two shapes that share the
header whitelist (`build_upstream_headers` in `_anthropic_common.py`)
and the best-effort recording philosophy:

- **Non-streaming** (`capture/anthropic.py`): single buffered POST →
  parse the JSON response → one `record_event(...)` call → return the
  response verbatim. Non-2xx responses are forwarded but never write
  a row (no usage data on an error envelope).
- **Streaming** (`capture/anthropic_streaming.py`): `client.send(req,
  stream=True)`; tee upstream bytes to `StreamingResponse` while a
  line-buffered SSE parser accumulates `message_start.usage` (inputs +
  cache + `msg_id`) and `message_delta.usage` (final cumulative
  outputs). One row on clean completion; one `success=False` row with
  a fixed `error_type` enum value on mid-flight failure.

### Streaming capture: partial-count semantics

Output tokens on failed streams reflect the last `message_delta`
observed and may underreport actual billing. The recorder contract is
in `capture/anthropic_streaming.py:_write_event` — `success=False`
rows carry an `error_type` from the closed enum
(`stream_interrupted | upstream_error | client_disconnect |
connection_dropped | timeout | parse_error`) and `request_id=NULL`
(so a successful retry with a fresh `msg_…` can't UNIQUE-conflict
with the recorded failure). The asymmetric rule: a row is only
written when `message_start` was observed; without it we have no
model name and no input/cache counts to honestly record.

## Spend tools: failure rows are hidden by default

`query_spend` and `usage_summary` both accept `include_failed: bool =
False`. Default behavior excludes `success=False` rows from totals,
per-group rollups, top-N rankings, and the `largest_call` lookup —
because partial-stream rows aren't trustworthy spend numbers (we
can't be sure whether Anthropic billed for the partial output).
Passing `include_failed=True` brings them back uniformly across every
leg of the result, which is useful for debugging capture-layer
behavior but not for honest spend reporting.
