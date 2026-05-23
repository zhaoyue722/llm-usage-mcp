# llm-usage-mcp

a local-first, multi-provider tool that captures LLM API spend and exposes it to coding agents via the Model Context Protocol

[![CI](https://github.com/zhaoyue722/llm-usage-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/zhaoyue722/llm-usage-mcp/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.13+](https://img.shields.io/badge/python-3.13%2B-blue.svg)](https://www.python.org/downloads/)

## Why

A coding agent today calls LLMs from several providers — Anthropic, OpenAI, plus Chinese providers like Qwen and DeepSeek. Costs end up scattered across dashboards, billed in different currencies, with different cache-pricing models. `llm-usage-mcp` gives you **one local SQLite of every LLM call you make**, exposed as MCP tools so any coding agent can answer "how much did I spend on Claude this week?", "which provider is cheapest for this task?", or "recommend a provider given my budget."

- **Local-first.** No SaaS, no signup, no telemetry. SQLite file in `~/.llm-usage/usage.db`. Privacy is a feature.
- **Multi-provider.** Anthropic, OpenAI, DeepSeek, Qwen — non-streaming and SSE streaming for every one.
- **MCP-native.** Reads + writes through the Model Context Protocol so Claude Code, Cursor, and any other MCP client get the same surface.

## Quickstart

Two minutes from `git clone` to "Claude Code can answer how much I just spent."

### 1. Install

```bash
git clone https://github.com/zhaoyue722/llm-usage-mcp.git
cd llm-usage-mcp
uv sync
```

`uv sync` installs the project + dev deps and creates two console scripts in the venv:
- `llm-usage-proxy` — the capture proxy (Layer 1).
- `llm-usage-mcp` — the MCP server (Layer 3).

### 2. Set at least one API key

You only need a key for the provider(s) you actually use; the proxy starts regardless and per-route requests return `503 configuration_error` for any provider whose key is missing.

```bash
export ANTHROPIC_API_KEY=sk-ant-...
# and/or:
export OPENAI_API_KEY=sk-...
export DEEPSEEK_API_KEY=sk-...
export DASHSCOPE_API_KEY=sk-...   # Qwen
```

Full env-var reference: [`docs/configuration.md`](docs/configuration.md) (or copy [`.env.example`](.env.example) to `.env` and fill in).

### 3. Run the capture proxy

```bash
uv run llm-usage-proxy
```

It binds **loopback-only** (`127.0.0.1:5525`) — never reachable from the network. The proxy holds your API keys server-side; clients never need them.

### 4. Point your coding agent at the proxy

The proxy exposes one route per provider. Set the matching `*_BASE_URL` env var on the client side:

| Provider | Client env var | Value |
|---|---|---|
| Anthropic | `ANTHROPIC_BASE_URL` | `http://127.0.0.1:5525` |
| OpenAI | `OPENAI_BASE_URL` | `http://127.0.0.1:5525/openai/v1` |
| DeepSeek | `DEEPSEEK_BASE_URL` (or any OpenAI-SDK base-url override) | `http://127.0.0.1:5525/deepseek/v1` |
| Qwen | DashScope OpenAI-compatible base | `http://127.0.0.1:5525/qwen/v1` |

Example — launch Claude Code with calls routed through the proxy:

```bash
ANTHROPIC_BASE_URL=http://127.0.0.1:5525 claude
```

Every call lands in `~/.llm-usage/usage.db` with tokens, cost, latency, and a `request_id` for idempotency.

### 5. Query your spend via MCP

Register the MCP server with Claude Code:

```bash
claude mcp add llm-usage -- uv --directory $(pwd) run llm-usage-mcp
```

Then ask Claude inside that session:

> How much did I spend on Anthropic today? Which provider is cheapest for a 10k-input / 2k-output call?

Claude calls `usage_summary`, `query_spend`, `compare_providers`, and friends under the hood.

## MCP tools

Seven tools, exposed over stdio. Full param/return shapes in [`docs/spec.md`](docs/spec.md).

| Tool | Purpose |
|---|---|
| `query_spend` | Totals + per-group rollups over a time window (group by provider / model / project / tag / day). |
| `usage_summary` | Headline summary for `today` / `week` / `month` / `year` — totals, top-N providers + models, largest call. |
| `compare_providers` | Given a hypothetical workload (tokens in / out), rank every priced model by cost. |
| `recommend_provider` | Pick the cheapest priced model that fits a stated budget. |
| `get_pricing` | Inspect the vendored pricing snapshot. |
| `list_providers` | List providers + their models + OpenAI-compatibility flag. |
| `record_usage` | Manual write path — log a call when the capture proxy isn't in the picture. |

`query_spend` and `usage_summary` default to `include_failed=false` so partial-stream rows don't pollute totals; opt-in via the param.

## Architecture

```
Layer 3:  src/llm_usage/mcp/       — MCP tools + resources (read)
Layer 2:  src/llm_usage/core/      — SQLite + pricing + cost math
Layer 1:  src/llm_usage/capture/   — proxy: Anthropic, OpenAI, DeepSeek, Qwen
```

One SQLite (`~/.llm-usage/usage.db`) sits in the middle. The proxy and the MCP server are independent processes that happen to share the file. Costs are snapshotted into `usage_events` at write time, so a future pricing change never rewrites history.

Streaming capture works by teeing upstream SSE bytes to the client unchanged while a side-channel parser accumulates the `usage` block — Anthropic and OpenAI families have different wire formats and slightly different recording semantics, both documented in [`docs/architecture.md`](docs/architecture.md).

## Supported providers (v1)

| Provider | Auth | Non-streaming | Streaming | Cache pricing |
|---|---|---|---|---|
| Anthropic | `x-api-key` | yes | yes | `cache_creation` + `cache_read` |
| OpenAI | `Bearer` | yes | yes | nested `prompt_tokens_details.cached_tokens` |
| DeepSeek | `Bearer` | yes | yes | `prompt_cache_hit_tokens` / `_miss_tokens` |
| Qwen (DashScope) | `Bearer` | yes | yes | usually omitted on the OpenAI-compat endpoint |

Pricing data is a vendored, trimmed snapshot of [LiteLLM's pricing JSON](https://github.com/BerriAI/litellm/blob/main/litellm/model_prices_and_context_window_backup.json), refreshed weekly by a GitHub Action ([`refresh-pricing.yml`](.github/workflows/refresh-pricing.yml)).

## Configuration

All config lives in env vars (or a `.env` file at the repo root). Defaults are sane; nothing is required to start the proxy. See [`docs/configuration.md`](docs/configuration.md) for the full reference. The most common knobs:

| Variable | Default | Purpose |
|---|---|---|
| `LLM_USAGE_DB_URL` | `sqlite:///$HOME/.llm-usage/usage.db` | Where the local DB lives. |
| `LLM_USAGE_PROXY_PORT` | `5525` | Capture proxy port (loopback only). |
| `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / `DEEPSEEK_API_KEY` / `DASHSCOPE_API_KEY` | unset | Provider keys; only what you use is required. |

## Development

```bash
uv sync                                  # install project + dev deps
uv run pytest                            # 355 tests, ~3s
uv run ruff check src/ tests/            # lint
uv run ruff format --check src/ tests/   # formatting
uv run mypy                              # --strict
```

CI runs all four on every PR and every push to `main` ([`.github/workflows/ci.yml`](.github/workflows/ci.yml)) with an 80% coverage floor.

## License

[MIT](LICENSE).
