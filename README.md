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

`uv sync` installs the project + dev deps and creates three console scripts in the venv:
- `llm-usage` — the multi-command CLI. See [CLI](#cli) below.
- `llm-usage-mcp` — the stdio MCP server (Layer 3).
- `llm-usage-proxy` — a back-compat alias; identical to `llm-usage proxy`.

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

## CLI

The CLI mirror of the MCP tools — seven subcommands under one `llm-usage` console, for when typing is faster than asking your agent.

```text
$ llm-usage
 Local-first LLM spend capture + query, exposed over MCP.

 Commands
   proxy      Run the local LLM capture proxy on 127.0.0.1.
   compare    Project the cost of a hypothetical workload across every priced model.
   models     Browse the local pricing catalog.
   recommend  Recommend the cheapest priced model for a workload + budget.
   spend      Show recorded spend over a calendar period.
   status     Snapshot of the local install: DB, proxy, providers, pricing.
   providers  List configured providers with key state, wire-format, model count.
```

| Command | The question it answers |
|---|---|
| [`compare`](#compare) | Given a workload, who's cheapest? |
| [`models`](#models) | What do they actually charge per million tokens? |
| [`recommend`](#recommend) | Just pick one for me. |
| [`spend`](#spend) | How much did I just spend? |
| [`status`](#status) | Is everything wired up correctly? |
| [`providers`](#providers) | What's configured locally? |
| `proxy` | Run the capture proxy (same as `llm-usage-proxy`). |

Conventions that hold across every command:

- `--json` emits the same Pydantic shape the matching MCP tool returns. Pipe straight into `jq`.
- `--color {auto,always,never}` honors `NO_COLOR` and TTY detection. The palette is a warm, low-contrast dark theme — easy on the eyes at 11pm.
- Filter flags (`--provider`, `--model`) are case-insensitive on providers, case-sensitive on models, and repeatable where they act as whitelists.
- `--version` / `-V` prints the version and exits. `--install-completion {bash|zsh|fish|powershell}` installs a tab-completion script — one shell restart later, every flag is `<Tab>`-able.

#### `compare`

Rank every priced model by projected cost for an `n`-input / `m`-output call. Cheapest first, percent against the cheapest. Default view family-deduplicates rows that share both a model family root *and* an identical price — so `gpt-5-mini` and `gpt-5-mini-2025-08-07` collapse to one row with `×2`. Pass `--all` to see every catalog row.

```bash
# How does an 8k-in / 2k-out call price out today?
$ llm-usage compare --in 8000 --out 2000

# Just OpenAI's models:
$ llm-usage compare --in 8000 --out 2000 --model gpt-5-mini --model gpt-5-nano

# Same projection, JSON for a script:
$ llm-usage compare --in 8000 --out 2000 --json | jq '.ranked[0]'
```

#### `models`

Catalog browser. Sibling of `compare`, but answers "what does this model charge?" rather than "what would my workload cost?". Rates per million tokens, sorted alphabetically by provider by default; switch with `--sort input` or `--sort output` to find the cheapest in either axis. Cache rates are hidden until you ask (`--cache`) because most models don't have them and empty columns waste width.

```bash
# Full catalog, deduped.
$ llm-usage models

# OpenAI's nano models only, with cache rates:
$ llm-usage models --provider openai --match nano --cache

# Cheapest input rate first — quick "what's the floor right now?":
$ llm-usage models --sort input
```

#### `recommend`

Picks one. Filters by `--provider`, `--model`, and `--budget`, then returns the cheapest match plus two runner-ups. The reasoning string explains what it assumed and what got chosen, so you can sanity-check rather than trust blindly.

```bash
# Cheapest priced model, full stop.
$ llm-usage recommend

# Anything Anthropic that fits under one cent for a 1k/1k call:
$ llm-usage recommend --provider anthropic --budget 0.01

# Of these three specific candidates, which wins?
$ llm-usage recommend --model gpt-5-mini --model claude-sonnet-4-6 --model qwen-max
```

v1 ranks by cost only. `--task` is optional and surfaces in the reasoning text; it doesn't drive selection (the tool isn't an LLM and can't interpret free text).

#### `spend`

Read the SQLite. The default view is a `usage_summary` headline — total dollars, top-3 providers, top-3 models, largest single call. Pass `--group-by` to switch into rollup mode.

```bash
# Headline for this week.
$ llm-usage spend

# This month grouped by model, JSON for a dashboard:
$ llm-usage spend --period month --group-by model --json | jq

# Spend on a specific project tag, day-by-day:
$ llm-usage spend --group-by day --project my-side-thing
```

Period boundaries are calendar UTC: `today` = since 00:00 UTC, `week` = since Monday, `month` = since the 1st, `year` = since January 1st. Failed / partial-stream rows are excluded by default; opt in with `--include-failed`.

#### `status`

One screen, four sections: Database, Capture proxy, Providers, Pricing. The "is everything actually working?" command. Read-only — running it on a fresh install before you've ever booted the proxy or MCP server prints `database not initialized` rather than silently creating the file.

```bash
$ llm-usage status

# Skip the network probe (offline, CI, slow link):
$ llm-usage status --no-net

# Machine-readable for a healthcheck script:
$ llm-usage status --json
```

#### `providers`

Per-provider configuration view. Wider than the `status` Providers block: adds the wire-format flag (`openai-compat: yes/no`) and an optional `--models` expansion that lists every priced model under each provider.

```bash
$ llm-usage providers
$ llm-usage providers --models   # expand each provider with its model list
```

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
uv run pytest                            # 700+ tests, ~6s
uv run ruff check src/ tests/            # lint
uv run ruff format --check src/ tests/   # formatting
uv run mypy                              # --strict
```

CI runs all four on every PR and every push to `main` ([`.github/workflows/ci.yml`](.github/workflows/ci.yml)) with an 80% coverage floor.

## License

[MIT](LICENSE).
