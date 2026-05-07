# Project A — Specification

## Project Vision (the one-paragraph version)

> A coding agent today calls LLMs from many providers — Anthropic, OpenAI, Google, AWS Bedrock, plus Chinese providers like Qwen, DeepSeek, Moonshot. Costs are scattered across dashboards, billed in different currencies, with different cache pricing models. This project gives you one local SQLite of every LLM call you make, exposed as MCP tools so any coding agent can answer "how much did I spend on Claude this week?", "which provider is cheapest for this task?", or "recommend a provider given my budget and quality priority". Local-first (no SaaS), provider-agnostic (OpenAI-compatible adapters), and the only one with first-class Chinese-provider support.

### Why it matters

- **Universal pain point**: every team using multiple LLMs has this problem.
- **Underserved**: existing tools (LiteLLM, Helicone, LangSmith) are either SaaS-only, framework-coupled, or Western-only.
- **First-mover MCP positioning**: very few cost/observability MCPs exist as of mid-2026; the marketplace listings will rank you fast.
- **Coherent narrative for your career**: billing → metering → cost-telemetry-for-AI is one storyline.

---

## Architecture Decision (v1)

The product has **three layers**. Build them all in v1; keep each layer minimal.

```
┌─────────────────────────────────────────────────────────────┐
│  Layer 3: MCP Server                                         │
│   Tools: record_usage, query_spend, compare_providers,       │
│          recommend_provider, get_pricing, usage_summary      │
└──────────────────────────┬──────────────────────────────────┘
                           │ reads/writes
┌──────────────────────────▼──────────────────────────────────┐
│  Layer 2: Core Library + SQLite                              │
│   - usage events table (provider, model, tokens, cost, ...)  │
│   - pricing table (vendored JSON, refreshable)               │
│   - cost calculator (handles cache pricing per provider)     │
└──────────────────────────┬──────────────────────────────────┘
                           │ writes
┌──────────────────────────▼──────────────────────────────────┐
│  Layer 1: Capture (two paths in v1)                          │
│   - Path A: OpenAI-compatible HTTP proxy (drop-in)           │
│   - Path B: Python SDK wrappers (anthropic, openai)          │
│   - Path C (post-v1): hooks for Claude Code                  │
└─────────────────────────────────────────────────────────────┘
```

**Key decisions to lock in early**:

1. **Local-first**. No cloud, no telemetry, no signup. SQLite file in `~/.llm-usage/usage.db`. Privacy is a feature.
2. **Python first, Go later**. Python ships faster; the MCP Python SDK is mature; the OpenAI / Anthropic / Google SDKs are first-class in Python.
3. **OpenAI-compatible by default**. Most Western and Chinese providers expose OpenAI-compatible endpoints. One adapter covers many providers.
4. **Pricing is data, not code**. Vendor a JSON pricing file (the `model_prices.json` pattern that LiteLLM uses); refresh via GitHub Action weekly.
5. **MCP tools mirror real questions**, not CRUD. Don't expose database internals — expose questions a developer would ask.

### What's NOT in v1 (resist these)

- A web UI / dashboard
- Multi-user / team mode
- Anthropic / OpenAI billing-API import (just use captured proxy data)
- Cloud sync / SaaS mode
- Real-time alerts / budget thresholds
- LangChain / LlamaIndex / framework integrations beyond the basic SDK wrappers
- Hosted demo

Each of these is a Year-2 feature.

## Tech Stack

| Layer | Choice | Why |
|---|---|---|
| Language | **Python 3.11+** | MCP SDK is best-in-class; LLM SDKs are first-class; faster to ship |
| Package manager | **uv** | Fast, modern, and the course mentor's standard |
| MCP framework | **Official `mcp` package** | Don't roll your own |
| HTTP framework (proxy) | **FastAPI + Uvicorn** | Battle-tested, async, streaming support |
| Database | **SQLite via sqlite3 stdlib** + **`sqlalchemy[asyncio]`** for migrations | Local-first, no infra |
| Schema migrations | **Alembic** | Standard with SQLAlchemy |
| Test runner | **pytest** + **pytest-asyncio** | Standard |
| HTTP mocking for tests | **respx** (async) or **vcrpy** | Async-friendly mocks |
| Linter / formatter | **ruff** | Fast, all-in-one |
| Type checker | **mypy --strict** | Senior signal |
| Packaging | **uv build** + **PyPI** | Single command publish |
| CLI | **typer** | Clean CLI with type hints |
| Docs | **Markdown in repo** + (stretch) **MkDocs Material** for docs site | Minimal |

### Repo structure

```
llm-usage-mcp/
├── README.md               # English; quickstart in <2 minutes
├── README.zh.md            # Chinese version
├── LICENSE                 # MIT or Apache 2.0
├── CHANGELOG.md
├── CLAUDE.md               # The agent context for THIS project (eat your own dog food)
├── pyproject.toml
├── uv.lock
├── .github/
│   └── workflows/
│       ├── ci.yml          # tests + lint
│       └── refresh-pricing.yml  # weekly cron
├── src/
│   └── llm_usage/
│       ├── __init__.py
│       ├── core/
│       │   ├── db.py            # SQLAlchemy models + session
│       │   ├── pricing.py       # cost calculator
│       │   ├── models.py        # Pydantic types
│       │   └── pricing_data/
│       │       └── prices.json  # vendored
│       ├── capture/
│       │   ├── proxy.py         # FastAPI proxy
│       │   └── wrappers.py      # SDK wrappers
│       ├── mcp/
│       │   ├── server.py        # MCP entrypoint
│       │   └── tools.py         # tool implementations
│       └── cli.py               # typer CLI
├── tests/
│   ├── unit/
│   ├── integration/
│   └── fixtures/
│       └── sample_responses/    # per-provider JSON
├── docs/
│   ├── architecture.md
│   ├── adding_a_provider.md
│   └── adding_a_provider.zh.md
└── examples/
    ├── claude_code_setup.md
    ├── cursor_setup.md
    └── direct_python.py
```

---

## Database Schema (lock this in Day 2)

```sql
CREATE TABLE usage_events (
    id              TEXT PRIMARY KEY,         -- uuid7
    timestamp       INTEGER NOT NULL,          -- ms epoch
    provider        TEXT NOT NULL,             -- "anthropic", "openai", "qwen", ...
    model           TEXT NOT NULL,             -- "claude-sonnet-4-6", ...
    input_tokens    INTEGER NOT NULL DEFAULT 0,
    output_tokens   INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens  INTEGER NOT NULL DEFAULT 0,
    cost_nano_usd   INTEGER NOT NULL,           -- nano-USD (10^-9), pre-computed at insert
    duration_ms     INTEGER,
    success         INTEGER NOT NULL DEFAULT 1,
    error_type      TEXT,
    request_id      TEXT,                      -- provider's id, for dedup
    project         TEXT,                      -- user-tagged context
    tags            TEXT,                      -- JSON array
    metadata        TEXT                       -- JSON object, free-form
);

CREATE INDEX idx_events_timestamp ON usage_events(timestamp);
CREATE INDEX idx_events_provider_model ON usage_events(provider, model);
CREATE INDEX idx_events_project ON usage_events(project);
CREATE UNIQUE INDEX idx_events_request_id ON usage_events(request_id) WHERE request_id IS NOT NULL;

CREATE TABLE pricing_snapshot (
    -- mirror of vendored JSON, materialized for queryability
    provider        TEXT NOT NULL,
    model           TEXT NOT NULL,
    input_per_million_usd       REAL NOT NULL,
    output_per_million_usd      REAL NOT NULL,
    cache_write_per_million_usd REAL,
    cache_read_per_million_usd  REAL,
    fetched_at      INTEGER NOT NULL,
    PRIMARY KEY (provider, model)
);

CREATE TABLE schema_version (version INTEGER PRIMARY KEY);
INSERT INTO schema_version VALUES (1);
```

**Why these specific design choices**:

- **`request_id` UNIQUE WHERE NOT NULL**: enables idempotent recording — your AWS billing reflex applied here. Replaying a log file won't double-count.
- **`cost_nano_usd` (INTEGER nano-USD = 10⁻⁹ USD), precomputed at insert**: snapshot pricing at the moment of the call. Pricing changes don't retroactively rewrite history. (Event-sourcing principle.) Stored as integer for exact aggregate arithmetic — `SUM(cost_nano_usd)` over millions of rows never drifts. MCP tools convert to float USD at the API boundary (`cost_nano_usd / 1e9`).
- **`tags` and `metadata` as JSON columns**: SQLite supports JSON1 extension; query with `json_extract(...)`. Keeps schema small.
- **`project`** as a top-level column (not in metadata): it's a primary group-by axis; deserves an index.

---

## MCP Tool Specification (the public API)

This is the contract. Lock this in Day 2; build against it.

### `record_usage`

Records a single LLM call.

```yaml
description: Record a single LLM API call with token counts.
            Cost is computed automatically from the pricing table.
parameters:
  provider:        string  required   # "anthropic" | "openai" | "qwen" | ...
  model:           string  required   # e.g. "claude-sonnet-4-6"
  input_tokens:    integer required
  output_tokens:   integer required
  cache_write_tokens: integer optional (default 0)
  cache_read_tokens:  integer optional (default 0)
  duration_ms:     integer optional
  success:         boolean optional (default true)
  error_type:      string  optional
  request_id:      string  optional   # for idempotency
  project:         string  optional
  tags:            string[] optional
  metadata:        object  optional
returns:
  id:              string              # uuid of the recorded event
  cost_usd:        number              # computed cost in float USD (cost_nano_usd / 1e9)
  warning:         string|null         # e.g. "model not in pricing table; cost set to 0"
```

### `query_spend`

Returns spending broken down by a chosen axis over a time window.

```yaml
parameters:
  start:           ISO-8601 string optional  # default: 30 days ago
  end:             ISO-8601 string optional  # default: now
  group_by:        enum optional             # "provider" | "model" | "project" | "tag" | "day"
                                             # default: "provider"
  filter:          object optional           # {provider?: string, model?: string, project?: string}
returns:
  total_cost_usd:  number
  total_calls:     integer
  total_input_tokens:  integer
  total_output_tokens: integer
  groups: [
    {
      key: string,                   # e.g. "anthropic"
      cost_usd: number,
      calls: integer,
      input_tokens: integer,
      output_tokens: integer,
    }
  ]
```

### `compare_providers`

Given an estimated workload, projects cost across all known providers/models.

```yaml
parameters:
  expected_input_tokens:  integer required
  expected_output_tokens: integer required
  task_type:              enum optional      # "chat" | "code" | "reasoning" | "extraction"
  models:                 string[] optional  # restrict to these
  include_cached_estimate: boolean optional  # default false
returns:
  ranked: [
    {
      provider:           string,
      model:              string,
      cost_usd:           number,
      relative_cost_pct:  number,            # vs cheapest = 100%
      notes:              string|null,        # "cache pricing applied", etc.
    }
  ]
```

### `recommend_provider`

Single-best recommendation given priorities.

```yaml
parameters:
  task_description:  string required
  expected_input_tokens:  integer optional
  expected_output_tokens: integer optional
  budget_usd:        number optional
  quality_priority:  enum optional   # "lowest_cost" | "balanced" | "highest_quality"
returns:
  provider:          string
  model:             string
  estimated_cost_usd: number
  reasoning:         string          # natural-language explanation
```

### `get_pricing`

```yaml
parameters:
  provider: string optional
  model:    string optional
returns:
  models: [
    { provider, model, input_per_million_usd, output_per_million_usd,
      cache_write_per_million_usd, cache_read_per_million_usd, fetched_at }
  ]
```

### `usage_summary`

```yaml
parameters:
  period: enum optional  # "today" | "week" | "month" | "year"  default: "week"
returns:
  period:           string
  total_cost_usd:   number
  call_count:       integer
  top_providers:    [{ provider, cost_usd, pct }]
  top_models:       [{ model, cost_usd, pct }]
  largest_call:     { id, model, cost_usd, timestamp }
```

### `list_providers`

```yaml
returns:
  providers: [
    { name: string, models: string[], openai_compatible: boolean }
  ]
```

### Resources (the "data" side of MCP)

- `usage://summary/current-month` — markdown summary, useful for the agent to drop into chat
- `usage://summary/last-7-days` — same

## Provider quirks (one-liners; full details: [Provider_Adapter_Reference.md](./Provider_Adapter_Reference.md))

- **Anthropic**: streaming usage split across `message_start` + `message_delta`
- **OpenAI**: streaming needs `stream_options.include_usage=true`; `choices=[]` on usage chunk
- **Qwen**: OpenAI-compatible; pricing in CNY needs FX conversion
- **DeepSeek**: `prompt_cache_hit_tokens` / `prompt_cache_miss_tokens` (not OpenAI-style)
