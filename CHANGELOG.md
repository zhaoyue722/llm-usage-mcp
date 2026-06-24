# Changelog

All notable changes to `llm-usage-mcp` are recorded here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

Nothing yet.

## [0.1.0] — 2026-06-24

The initial public release: local-first MCP server that captures LLM API spend across Anthropic, OpenAI, DeepSeek, and Qwen, and exposes spend / pricing / recommendation queries as MCP tools.

### Added

#### Layer 1 — Capture proxy

- FastAPI proxy bound to loopback only (`127.0.0.1:5525`) — the bind host is a constant, not a flag, so a misconfiguration can't expose the proxy to the network.
- Anthropic `/v1/messages` route, **non-streaming** — forwards to `api.anthropic.com`, parses `usage.input_tokens` / `usage.output_tokens` / `usage.cache_creation_input_tokens` / `usage.cache_read_input_tokens`, writes one `usage_events` row on 2xx.
- Anthropic `/v1/messages` route, **SSE streaming** — tees upstream bytes to the client unchanged while a side-channel parser accumulates `message_start.usage` (input + cache + `msg_id`) and `message_delta.usage` (final cumulative output). Writes one `success=True` row on clean completion or one `success=False` row with a typed `error_type` on mid-flight failure.
- OpenAI-compatible `/{provider}/v1/chat/completions` routes for **OpenAI, DeepSeek, Qwen** (DashScope compatible mode), both **non-streaming and SSE streaming**. Per-provider token extractors normalize the three different cache-token shapes into the schema's `input` / `cache_write` / `cache_read` fields.
- `503 configuration_error` envelope on routes whose API key isn't set — the proxy starts regardless of which keys are configured, so a user dogfooding one provider doesn't have to set four keys upfront.
- Per-provider missing-key warnings logged on boot, above the migration `INFO` lines so they're visible.
- Pooled `httpx.AsyncClient` (one per process) for keepalive + TLS reuse across upstream calls.

#### Layer 2 — Core library + SQLite

- `usage_events` table (one row per LLM call), `pricing_snapshot` table (materialized vendored pricing), `pricing_tier` table (LiteLLM `tiered_pricing` brackets), `quality_snapshot` table (reserved for a future leaderboard importer; empty in v1), and `schema_version` — all under Alembic.
- Cost stored as **integer nano-USD** (`cost_nano_usd`, 10⁻⁹ USD) — exact aggregate arithmetic with INT64 headroom; conversion to float USD happens only at the MCP-tool boundary via `nano_to_usd`.
- `CostCalculator` — handles input + output + Anthropic-style cache tokens; raises rather than silently zeroing when cache tokens are present but no cache rate is in pricing.
- **Tier-aware pricing**: `CostCalculator` picks the input/output rate by the call's `input_tokens`, so prompt-size-tiered models (qwen-flash's `[0, 256k)` and `[256k, 1M)` brackets) bill correctly above 256k input.
- `request_id` idempotency with race-handled `IntegrityError` rollback (`UNIQUE WHERE request_id IS NOT NULL` partial index) — replaying a log file or retrying a webhook won't double-count.
- Vendored, trimmed snapshot of [LiteLLM's pricing JSON](https://github.com/BerriAI/litellm/blob/main/litellm/model_prices_and_context_window_backup.json) for v1's four providers; loader converts to the internal `Pricing` shape.
- `pricing_overrides.json` field-merged onto the LiteLLM snapshot at load time — closes catalog gaps (e.g. `deepseek-v4-flash`, which `deepseek-chat` aliases to in production) without forking LiteLLM. Re-materialized on every boot, so edits to the overrides file take effect on the next restart.
- SQLite WAL mode + `synchronous=NORMAL` applied automatically on file-backed engines.

#### Layer 3 — MCP server

- Stdio entrypoint (`llm-usage-mcp`) via the official `mcp[cli]` SDK's `FastMCP`. Verified end-to-end with `claude mcp add`.
- **Seven tools, all wired:**
  - `record_usage` — manual write path with cost computed at insert and `request_id` dedup.
  - `query_spend` — totals + per-group rollups (provider / model / project / tag / day) over a `[start, end)` window. Multi-tag events contribute once per tag; NULL tags and NULL projects are excluded from their respective group-by axes.
  - `usage_summary` — calendar-period rollup (today / week / month / year, UTC boundaries) with total cost, top-3 providers, top-3 models, and the single largest call.
  - `compare_providers` — projects a hypothetical workload cost across every priced model, returns ranked with `relative_cost_pct` vs the cheapest.
  - `recommend_provider` — picks the cheapest model that fits an optional `budget_usd`; falls back to the cheapest overall when nothing fits, with reasoning that says so plainly.
  - `get_pricing` — read the materialized `pricing_snapshot`, optional provider / model filters.
  - `list_providers` — providers + their models + OpenAI-compatibility flag.
- **Two resources:**
  - `usage://recent_events` — most recent 50 `usage_events` rows as JSON.
  - `usage://pricing_table` — full `pricing_snapshot` as JSON.
- `include_failed: bool = False` on `query_spend` and `usage_summary` — partial-stream rows (`success=False`) are excluded from totals, per-group rollups, top-N rankings, and the largest-call lookup unless explicitly opted in.
- ISO-8601 window parsing accepts trailing-`Z`, explicit offsets, and naive strings (interpreted as UTC) so results don't depend on where the server runs.

#### Layer 4 — CLI

- `llm-usage` multi-command CLI (Typer) with eight subcommands: `proxy`, `compare`, `models`, `recommend`, `spend`, `status`, `providers`, `about`. Each read command mirrors the matching MCP tool and shares the same `core/` layer, so the CLI and MCP stay in lockstep on ranking / aggregation semantics.
- Human-readable tables with a warm low-contrast palette (TTY- and `NO_COLOR`-aware) plus `--json` on every command, emitting the same Pydantic shapes the MCP tools return — pipe straight into `jq`.
- `--version` / `-V`, shell completion (`--install-completion {bash|zsh|fish|powershell}`), and `about` (version / author / license / homepage, read from installed package metadata).
- Three console scripts: `llm-usage`, `llm-usage-mcp`, and `llm-usage-proxy` (a back-compat alias for `llm-usage proxy`).
- "watch-pom" startup banner for the proxy (and, on a TTY, the MCP server).

#### Bootstrap / configuration

- `bootstrap()` runs `alembic upgrade head` programmatically and **re-materializes pricing on every boot** (idempotent upsert), so edits to `pricing_overrides.json` reach `pricing_snapshot` on the next restart.
- `Settings(BaseSettings)` owns every knob — DB URL, log level, proxy port, per-provider base URLs and API keys, enabled providers. Keys use `SecretStr` to keep them out of reprs.
- `.env.example` and `docs/configuration.md` cover the full reference.
- `Settings.require_keys(providers)` — refuse-to-start gate, called explicitly by capture-layer entry points; pure library / MCP imports stay usable without provider keys.

#### CI / infrastructure

- GitHub Actions CI workflow ([`ci.yml`](.github/workflows/ci.yml)) runs `ruff check`, `ruff format --check`, `mypy --strict`, and `pytest --cov --cov-fail-under=80` on every PR and push to main. Currently **731 tests passing**, project-wide coverage well above the 80% gate.
- Weekly pricing-refresh workflow ([`refresh-pricing.yml`](.github/workflows/refresh-pricing.yml)) — pulls the latest LiteLLM pricing JSON, re-trims to v1 providers, opens a PR if anything changed.
- MIT [LICENSE](LICENSE) and [CLAUDE.md](CLAUDE.md) for the project's own agent context (eating our own dog food).
- PyPI packaging metadata: SPDX `license = "MIT"`, bundled license file, and `[project.urls]` (Homepage / Repository / Issues) so the package page links back to the repo.

#### Documentation

- English [README.md](README.md) with logo lockup, two-minute quickstart, MCP tool table, full CLI reference, provider matrix, and configuration reference.
- Chinese [README.zh.md](README.zh.md) localized for readers in mainland China — leads with DeepSeek / Qwen, flags network-access considerations for Anthropic / OpenAI, notes the `pricing_overrides.json` escape hatch.
- [`docs/spec.md`](docs/spec.md) — the contract this release implements.
- [`docs/architecture.md`](docs/architecture.md) — three-layer breakdown and streaming-capture semantics (including partial-count rules for failed streams).
- [`docs/Provider_Adapter_Reference.md`](docs/Provider_Adapter_Reference.md) — per-provider auth, endpoints, token-field mapping, cache-pricing quirks.
- [`docs/configuration.md`](docs/configuration.md) — full env-var reference.
- [`docs/post_v1_providers.md`](docs/post_v1_providers.md) — effort estimates for Gemini / Bedrock / Moonshot / Zhipu / MiniMax / ERNIE.

### Known limitations

- **No SDK wrappers (Path B) in v1.** The spec sketches `wrap_anthropic(client)` / `wrap_openai(client)` shims; v1 ships proxy-only (Path A). Manual capture is available via the `record_usage` MCP tool for callers that can't or don't want to route through the proxy.
- **`recommend_provider` is cost-only.** The original spec accepted a `quality_priority` axis backed by a `quality_snapshot` table. The table is created by migration but kept empty in v1 — the only quality data we had was hand-authored editorial estimates, which would have been dishonest. A post-v1 release wires a real leaderboard importer; the surface returns the `quality_priority` parameter at that point.
- **`task_description` doesn't drive selection.** `recommend_provider` echoes it into the `reasoning` string but doesn't interpret it — the tool isn't an LLM.
- **`compare_providers.notes` is always `None`.** Field is reserved for future per-row caveats.
- **Bedrock pricing is not region-aware.** The `pricing_snapshot` schema doesn't carry a region column. Out of v1 scope; revisit when Bedrock support lands.

[Unreleased]: https://github.com/zhaoyue722/llm-usage-mcp/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/zhaoyue722/llm-usage-mcp/releases/tag/v0.1.0
