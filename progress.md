# Progress Log

> Project: **llm-usage-mcp** — a local-first, multi-provider tool that captures LLM API spend and exposes it to coding agents via the Model Context Protocol.

---

## English Version

### 2026-05-04 — Project Bootstrap

#### Goal
Stand up a clean Python project skeleton with modern tooling: **uv** for dependency management, **ruff** for lint/format, **mypy** for type checking, **pytest** for tests.

#### Initial State
The repository already contained:
- `.git/` (initialized git repo)
- `.gitignore`
- `LICENSE`
- `README.md` (one-line project description)

No Python source, no `pyproject.toml`, no virtual environment.

#### Step 1 — Initialize the uv project
Used the **package** layout (rather than a flat script) so we get a proper `src/` tree from the start:

```bash
uv init --package --name llm-usage-mcp
```

This created:
- `pyproject.toml` — minimal, with `requires-python = ">=3.13"` and a `llm-usage-mcp = "llm_usage_mcp:main"` console script.
- `.python-version` — pinned to `3.13`.
- `src/llm_usage_mcp/__init__.py` — stub `main()` function.

#### Step 2 — Add dev dependencies
Added `ruff`, `mypy`, `pytest`, and `pytest-cov` to the `dev` dependency group:

```bash
uv add --dev ruff mypy pytest pytest-cov
```

`uv` created the `.venv/` automatically and resolved everything into `uv.lock`. Resulting versions:
- `ruff==0.15.12`
- `mypy==1.20.2`
- `pytest==9.0.3`
- `pytest-cov==7.1.0`

#### Step 3 — Configure the tools in `pyproject.toml`
All tool config lives in `pyproject.toml` (single source of truth). Highlights:

- **Ruff**
  - `line-length = 100`, `target-version = "py313"`
  - Lint rule sets: `E`, `W`, `F`, `I`, `B`, `C4`, `UP`, `SIM`, `RUF`, `N`, `TID`
  - `tests/**/*.py` allows `assert` (`S101` ignored)
  - Format: double quotes, space indent

- **Mypy** — strict mode
  - `strict = true`, `warn_return_any`, `disallow_untyped_defs`, etc.
  - `tests.*` relaxed to allow untyped test helpers

- **Pytest**
  - `testpaths = ["tests"]`
  - `--strict-markers`, `--strict-config`
  - `filterwarnings = ["error"]` — warnings fail the suite (catches deprecation early)

- **Coverage**
  - `source = ["src"]`, `branch = true`
  - Excludes `if TYPE_CHECKING:` and `raise NotImplementedError`

#### Step 4 — First smoke test
- Created `tests/__init__.py` and `tests/test_smoke.py` with a single `test_main_runs()` that just calls `main()`. The point isn't coverage — it's to prove the toolchain wires through end-to-end.

#### Step 5 — Verify everything
Ran the full toolchain locally; all green:

```bash
uv run ruff check .       # All checks passed!
uv run ruff format --check .  # 3 files already formatted
uv run mypy                   # Success: no issues found in 3 source files
uv run pytest -q              # 1 passed in 0.01s
```

#### Final layout
```
llm-usage-mcp/
├── .git/
├── .gitignore
├── .python-version
├── .venv/               (uv-managed, git-ignored)
├── LICENSE
├── README.md
├── progress.md          (this file)
├── pyproject.toml
├── src/
│   └── llm_usage_mcp/
│       └── __init__.py
├── tests/
│   ├── __init__.py
│   └── test_smoke.py
└── uv.lock
```

#### Useful commands going forward
```bash
uv sync                      # install / update all deps from uv.lock
uv run ruff check . --fix    # lint and auto-fix
uv run ruff format .         # apply formatting
uv run mypy                  # type-check
uv run pytest                # run tests
uv run pytest --cov          # run tests with coverage
uv add <pkg>                 # add runtime dep
uv add --dev <pkg>           # add dev dep
```

### 2026-05-04 — Project docs (CLAUDE.md, spec, plan, adapter reference)

#### Goal
Land the project's "agent context" docs so future Claude Code sessions in this repo immediately know **what to build, how to build it, and what not to do**.

#### What was added / changed
- **`CLAUDE.md`** — rewritten to a lean (≤30-line) version with five sections: *What this project is*, *Coding standards*, *Workflow rules*, *Provider quirks* (one-liners), and *Current focus*. References `@docs/spec.md`, `@plan.md`, and `@docs/Provider_Adapter_Reference.md`.
- **`docs/spec.md`** — full v1 specification: project vision, three-layer architecture (capture / core+SQLite / MCP server), what's *not* in v1, tech stack, repo structure, database schema, and the MCP tool API (`record_usage`, `query_spend`, `compare_providers`, `recommend_provider`, `get_pricing`, `usage_summary`, `list_providers`).
- **`docs/Provider_Adapter_Reference.md`** — concrete request/response shapes for the four v1 providers (Anthropic, OpenAI, Qwen/DashScope, DeepSeek), including streaming SSE patterns, cache-token field paths, cost formulas, pricing JSON entry templates, adapter implementation skeletons, and a per-provider verification checklist. **Renamed from `Providing_Adapter_Reference.md`** (typo fix) so the references in `CLAUDE.md` and `spec.md` resolve.
- **`plan.md`** — Day 1 morning checklist (cleaned of indentation noise). Day 2–5 marked TODO (the source content was truncated mid-sentence and needs to be filled in).

#### Open issues to resolve next
- **Python version mismatch.** `CLAUDE.md` and `spec.md` say *Python 3.11+*, but `pyproject.toml` is pinned to `requires-python = ">=3.13"` and ruff/mypy `target-version = "py313"`. Pick one and align all three places.
- **`plan.md` is truncated.** The Day 1 list ends at "Write plan.md with your Day 2–5 tasks as" and Day 2–5 is empty. Fill in.
- **MCP `record_usage` tool spec mentions `cache_write_tokens` for input** — that's Anthropic-specific. Document in adapter reference how OpenAI/Qwen/DeepSeek map (already partially done; cross-link from `spec.md` once stable).

### 2026-05-06 — SQLAlchemy models for the usage database

#### Goal
Land the persistence-layer schema. The spec lists three tables (`usage_events`, `pricing_snapshot`, `schema_version`) plus four indexes, but no Python code existed yet.

#### Why models first, no engine yet
The capture layer, pricing layer, and MCP-tools layer all read/write through these models. Defining them in SQLAlchemy 2.0 typed style — `DeclarativeBase` + `Mapped[...]` columns — makes them sync/async-agnostic, so the engine choice (sync `sqlite3` vs async `aiosqlite`) can be made independently when `db.py`'s session factory is added. Smaller diff, fewer commitments.

#### Package rename: `llm_usage_mcp` → `llm_usage`
The spec's repo layout puts the import package at `src/llm_usage/`, but the bootstrap had created `src/llm_usage_mcp/`. To match the spec verbatim:
- `git mv src/llm_usage_mcp src/llm_usage`
- Console-script entry retargeted: `llm-usage-mcp = "llm_usage:main"`.
- The uv build backend infers the module name from the *project* name (`llm-usage-mcp` → `llm_usage_mcp`), so the build broke until `[tool.uv.build-backend] module-name = "llm_usage"` was added to `pyproject.toml`.
- Existing smoke test updated to `from llm_usage import main`.

The distribution name (`llm-usage-mcp`) is unchanged — only the import package moved.

#### What landed
- **`src/llm_usage/core/db.py`** — three models mirroring the spec:
  - `UsageEvent` (table `usage_events`) — 16 columns, `id` primary key, four indexes.
  - `PricingSnapshot` — composite primary key `(provider, model)`.
  - `SchemaVersion` — single-column table; constant `CURRENT_SCHEMA_VERSION = 1`.
- **`src/llm_usage/core/__init__.py`** — re-exports.
- **`tests/test_models.py`** — nine tests covering table set, columns + nullability, index names + columns, partial unique on `request_id`, server-default behavior on raw INSERT, composite PK, and round-trip persistence.

#### Design choices worth noting
- **`metadata` column → Python attribute `event_metadata`.** SQLAlchemy reserves `Base.metadata`, so the column name stays `metadata` (per spec) but the ORM-side attribute is renamed to avoid the clash.
- **Token defaults use both `default=0` and `server_default=text("0")`.** The spec says `NOT NULL DEFAULT 0` at the SQL layer, so a raw `INSERT` that omits the columns must still succeed. A test asserts that.
- **Partial unique index on `request_id`** matches the spec (`UNIQUE WHERE request_id IS NOT NULL`) via `Index(..., unique=True, sqlite_where=text("request_id IS NOT NULL"))`. This is what enables idempotent recording — replaying a captured log won't double-count.
- **`Float` instead of `REAL`.** `sqlalchemy.Float` is the cross-DB real type and renders as `REAL` on SQLite — same on-disk shape, more idiomatic in 2.0.
- **No engine, no session factory yet.** Those belong in a separate change once the `~/.llm-usage/usage.db` path discovery and async story land together.

#### Verification
```bash
uv run ruff check .       # All checks passed!
uv run ruff format --check .
uv run mypy               # Success: no issues found in 6 source files
uv run pytest -q          # 9 passed
```

#### Open issues / follow-ups
- Engine + session factory (`get_engine()`, `SessionLocal`) — next change.
- Alembic init + initial revision so schema upgrades are tracked alongside `schema_version`.
- A Pydantic mirror of `UsageEvent` for the MCP `record_usage` tool's input/output (the spec uses Pydantic types throughout the MCP layer).

### 2026-05-06 — Alembic init and the initial migration

#### Goal
Stand up Alembic so the schema can evolve under version control, and lock the current models in as revision `278ba38a2efd` (`initial schema`).

#### Why now, before the engine factory
The models from the previous PR exist but nothing has ever materialized them on disk. Adding Alembic first means the very first time someone runs `alembic upgrade head`, the on-disk DB and `Base.metadata` are guaranteed to match — no risk of `Base.metadata.create_all()` and migrations drifting apart.

#### Layout
- `alembic.ini` at the repo root (standard scaffold).
- `alembic/env.py` configured to read `Base.metadata` from `llm_usage.core` and to set `render_as_batch=True` on SQLite (required so future ALTER TABLE migrations work via Alembic's batch mode).
- `alembic/versions/278ba38a2efd_initial_schema.py` — the autogenerated revision, reviewed and lightly edited.

#### Database URL resolution
`alembic.ini`'s `sqlalchemy.url` is left blank. `env.py` resolves the URL at runtime:
1. `$LLM_USAGE_DB_URL` if set (used by tests and CI).
2. Otherwise `sqlite:///~/.llm-usage/usage.db` per spec.

This keeps the spec default in code (so `alembic upgrade head` "just works" for end users) without making `alembic.ini` carry environment-specific paths.

#### How the initial migration was generated
```bash
LLM_USAGE_DB_URL='sqlite:///:memory:' uv run alembic revision --autogenerate -m "initial schema"
```
Pointing at `:memory:` gives autogenerate an empty database to diff against, so it emits the full `CREATE TABLE` set instead of comparing against whatever happens to be in `~/.llm-usage/usage.db`.

#### What autogenerate captured correctly (no edits needed)
- All three tables with the right column types and nullability.
- Composite primary key on `pricing_snapshot (provider, model)`.
- Server defaults `0` on all token columns and `1` on `success`.
- The four indexes — including the **partial unique index** `idx_events_request_id` with `sqlite_where=sa.text("request_id IS NOT NULL")`. This is the load-bearing piece that makes recording idempotent; the migration test asserts the `WHERE` clause survives in the DDL.

#### One manual addition: the `schema_version` seed
Alembic owns its own version table (`alembic_version`); the spec's `schema_version` table is a separate, user-facing "schema major" that tooling can read without depending on Alembic. The initial migration now ends with:
```python
op.execute(sa.text("INSERT INTO schema_version (version) VALUES (1)"))
```
Both tables coexist by design — they answer different questions.

#### Post-write hooks
`alembic.ini` is set to run `ruff check --fix` and `ruff format` over each newly generated revision file. Future `alembic revision --autogenerate` runs will produce lint-clean code without manual cleanup.

#### Tests (`tests/test_migrations.py`)
Five tests, all passing:
1. `upgrade head` produces the spec's table set (plus `alembic_version`).
2. The columns on every model match the columns on disk (catches drift).
3. The partial-unique `WHERE` clause is preserved in `sqlite_master`.
4. `schema_version` is seeded with `1`.
5. `downgrade base` removes all spec tables cleanly.

#### Verification
```bash
uv run ruff check .       # All checks passed!
uv run ruff format --check .
uv run mypy               # Success: no issues found in 7 source files
uv run pytest -q          # 14 passed
```

#### Open issues / follow-ups
- Engine + session factory and a `db_path()` helper that mirrors `env.py`'s URL resolution (so app code and Alembic agree on the file).
- A `llm-usage-mcp db upgrade` CLI subcommand that wraps `alembic upgrade head` for end-users.
- A pricing-data loader that populates `pricing_snapshot` from the vendored `prices.json` on first run.

### 2026-05-06 — Engine + session factory (sync), shared with Alembic

#### Goal
Land the runtime side of the persistence layer: an engine factory, a process-wide session, and SQLite WAL tuning. Sync only — async deferred until the FastAPI capture proxy actually starts.

#### Why sync now
SQLite is single-writer, so async wouldn't actually parallelize writes; its real benefit is "don't block the event loop". Today's callers (MCP tools, smoke tests) aren't on an event loop, and the FastAPI proxy is spec'd but not built. Sync today is dramatically simpler to write/test/debug; SQLAlchemy 2.0 lets us add `AsyncSession` against the **same models and same engine URL** later without a model rewrite. If/when the proxy lands, the right move is "add async alongside sync" — starting sync doesn't paint us into a corner.

#### Layout change: `core/db.py` → `core/db/` package
- `core/db/models.py` — moved verbatim from the old `core/db.py`.
- `core/db/session.py` — new: `resolve_db_url`, `create_engine`, `get_engine`, `get_session_factory`, `get_session`, `DEFAULT_DB_PATH`.
- `core/db/__init__.py` — re-exports both modules so existing `from llm_usage.core import ...` keeps working.

The spec puts both models and session in a single `db.py`; we split because `db.py` was already at 100+ lines and adding the engine plumbing would have pushed it past comfortable file size. Public import path (`from llm_usage.core import ...`) is unchanged.

#### What `session.py` provides
- **`resolve_db_url()`** — env var `LLM_USAGE_DB_URL` first, default `sqlite:///~/.llm-usage/usage.db`. **Now shared with `alembic/env.py`** so app code and migrations always talk to the same database. Removed the duplicate helper from `env.py`.
- **`create_engine(url=None)`** — factory. For file-backed SQLite URLs, ensures the parent dir exists (so first run on a clean machine doesn't crash) and registers a `connect` listener.
- **WAL pragma listener** — on every connect to a file-backed SQLite engine, runs `PRAGMA journal_mode=WAL` and `PRAGMA synchronous=NORMAL`. Standard pairing: WAL gives concurrent reads during writes; NORMAL skips fsync per commit (fsync still happens at WAL checkpoint), which is the durability sweet spot for a local dev tool. In-memory and non-SQLite URLs skip both behaviors.
- **`get_engine()` / `get_session_factory()` / `get_session()`** — lazy module-level singletons. First call builds; subsequent calls reuse.

#### Tests (`tests/test_session.py`)
Six passing tests:
1. `resolve_db_url` default path.
2. `resolve_db_url` env-var override.
3. `create_engine` creates parent directories for nested DB paths.
4. WAL mode + `synchronous=NORMAL` are actually applied on connect (the asserts read `PRAGMA journal_mode` and `PRAGMA synchronous`).
5. In-memory URLs (`sqlite:///:memory:`) skip WAL — `journal_mode` reports `memory`, not `wal`.
6. **End-to-end smoke test**: insert a `UsageEvent`, query it back, asserts the round-tripped row matches.

#### Test plumbing worth noting
The smoke test exercises the lazy singleton path (`get_engine`, `get_session_factory`). To do that without leaking a SQLite connection pool across tests — which `filterwarnings = ["error"]` would turn into a fatal `PytestUnraisableExceptionWarning` when the pool is later GC'd — there's a `reset_session_singletons` fixture that nulls and disposes the module globals around each test.

#### Verification
```bash
uv run ruff check .       # All checks passed!
uv run ruff format --check .
uv run mypy               # Success: no issues found in 10 source files
uv run pytest -q          # 20 passed (9 model + 5 migration + 6 session)
```

#### Open issues / follow-ups
- A `llm-usage-mcp db upgrade` CLI subcommand wrapping `alembic upgrade head`, plus an `init` step that ensures `~/.llm-usage/` exists before first migration.
- Pricing-data loader: read the vendored `prices.json` and upsert into `pricing_snapshot`.
- Pydantic models for the MCP tool boundary (`record_usage` input/output, etc.).

### 2026-05-06 — Pricing + CostCalculator (and a spec change: `cost_usd` → `cost_nano_usd`)

#### Spec change first
The original spec had `cost_usd REAL NOT NULL` on `usage_events`. While designing the calculator we changed the column to `cost_nano_usd INTEGER NOT NULL` (10⁻⁹ USD), and updated `docs/spec.md` accordingly. Reasons:

- **Exact aggregate arithmetic.** `SUM(cost_nano_usd)` over millions of rows is exact integer math; float `SUM` accumulates rounding error.
- **Sufficient resolution.** 1 cache-read token at $0.30/M ≈ $3 × 10⁻⁷ = 300 nano-USD. Cleanly representable. Cents would round to zero.
- **Headroom.** SQLite INTEGER is 64-bit signed → max ~$9.2 B. Vastly more than any user's spend.
- **Industry pattern.** Stripe/payment systems store money as integer minor units; nano-USD is just "go small enough for LLM micro-pricing".

The MCP tool boundary still reports float `cost_usd: number`. The conversion is a single `cost_nano_usd / 1e9` at the edge — so end-users see human-friendly USD, and storage stays exact.

#### Migration: `0002_cost_usd_to_cost_nano_usd`
SQLite can't `ALTER COLUMN` types in place; the migration uses three batch_alter phases so existing rows survive:

1. Add `cost_nano_usd INTEGER` (nullable, no default).
2. `UPDATE usage_events SET cost_nano_usd = CAST(cost_usd * 1000000000 AS INTEGER)`.
3. Tighten `cost_nano_usd` to `NOT NULL` and drop `cost_usd`.

Downgrade is the symmetric reverse (`cost_nano_usd / 1000000000.0`). Verified manually with a real row of `cost_usd = 0.0042` round-tripping to `cost_nano_usd = 4_200_000` and back.

#### `core/pricing.py`
- **`Pricing`** — frozen dataclass. Rates kept as `float` (per-million-USD often has decimals like $3.75 or $0.30); `fetched_at` carried through so we can debug "why is today's cost different from yesterday's?". `Pricing.from_orm(PricingSnapshot)` does the ORM→dataclass conversion.
- **`CostCalculator`** — bound to one `Pricing`. The single method is `cost_nano_usd(*, input_tokens, output_tokens, cache_write_tokens=0, cache_read_tokens=0) -> int`. The math is just `tokens × rate × 1_000` (rate is per-million-USD, ×1000 converts to per-token nano-USD), summed across the four token kinds, banker-rounded to int.
- **`get_pricing(session, provider, model) -> Pricing | None`** — fetches the row from `pricing_snapshot`. Returns `None` when missing (the MCP layer's `record_usage` will translate that into the `warning` field per spec).

#### Validation: strict on missing cache rates
If `cache_write_tokens > 0` but `cache_write_per_million_usd` is `None` (or the same for cache_read), `cost_nano_usd` raises `ValueError`. Reason: this is always either stale pricing data or a buggy adapter — silently zeroing the contribution would undercount cost without surfacing the problem. The error message names the provider/model so the data issue is debuggable from the stack trace alone. `cache_*_tokens = 0` with a `None` rate is fine — the cache cost just contributes 0.

Negative tokens also raise; we trust the calling code but not so much that we'll silently negate a refund.

#### Test data: real Anthropic rates
Tests use the actual Claude Sonnet 4.6 numbers ($3 / $15 / $3.75 / $0.30). They double as living documentation: a reader sees the typical magnitudes (cache-read is 0.10× input, cache-write is 1.25× input) without leaving the test file. The combined-session test mirrors the worked example we used to motivate caching: 100K cache-write + 900K cache-read + 1.5K input/output ≈ $0.6555 = 655_500_000 nano-USD.

#### Verification
```bash
uv run ruff check .       # All checks passed!
uv run ruff format --check .
uv run mypy               # Success: no issues found in 12 source files
uv run pytest -q          # 41 passed (9 model + 5 migration + 6 session + 21 pricing)
```

#### Open issues / follow-ups
- Pricing-data loader: read vendored `prices.json` and upsert into `pricing_snapshot` (idempotent on `(provider, model)` PK; bumps `fetched_at` on each refresh).
- `record_usage` MCP tool: wire token counts → `get_pricing` → `CostCalculator` → insert `UsageEvent`. Translate missing pricing into the spec's `warning` field.
- A `llm-usage-mcp db upgrade` CLI subcommand.

### 2026-05-07 — Vendored pricing data (LiteLLM JSON, trimmed)

#### Goal
Land the source of truth for pricing — `src/llm_usage/core/pricing_data/prices.json` — so the upcoming loader has data to populate `pricing_snapshot` from.

#### Source
LiteLLM's [`model_prices_and_context_window_backup.json`](https://raw.githubusercontent.com/BerriAI/litellm/main/litellm/model_prices_and_context_window_backup.json) — the same file LiteLLM uses internally for cost tracking. Spec called this out as the convention to follow ("the `model_prices.json` pattern that LiteLLM uses"). Refreshed weekly (manually for now; a GitHub Action is on the roadmap).

#### Why vendor LiteLLM's shape verbatim
Refresh stays a one-liner (download → `jq` filter → commit). Conversion to our `pricing_snapshot` shape happens once at load time in the loader; the committed JSON stays compatible with anyone else who parses LiteLLM data. We pay the conversion cost once, not on every refresh.

#### Filter applied
```jq
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
```

- **Providers**: anthropic, openai, deepseek, dashscope (Alibaba's API, the v1 path for Qwen). LiteLLM also exposes Anthropic via Bedrock and Vertex; v1 scope is direct provider APIs only, so those are excluded.
- **Modes**: `chat` and `responses` (OpenAI's Responses API). Embeddings, audio, image, moderation, video are excluded — pricing for those doesn't fit a per-million-token shape.
- **No-pricing entries**: dropped two metadata-only rows (`dashscope/qwen3-30b-a3b`, `openai/container`) that LiteLLM keeps for context but provides no rates for.

Final tally: **178 models**, ~150 KB. Per provider: anthropic 20, dashscope 32, deepseek 8, openai 118.

#### Tiered pricing oddity
A handful of Qwen models use `tiered_pricing` (a list of `{range, input_cost_per_token, output_cost_per_token}`) instead of flat rates — Alibaba bills different rates above token-count thresholds. The structural test allows either shape; the loader will need to pick a base rate (likely the first tier) when populating `pricing_snapshot`. Anthropic's `*_above_200k_tokens` variants are similar but show up alongside flat rates and are simpler to handle (use the flat rate; ignore the tier).

#### Files
- `src/llm_usage/core/pricing_data/prices.json` — the trimmed payload.
- `src/llm_usage/core/pricing_data/__init__.py` — empty; makes the directory a regular package so `importlib.resources.files(...)` resolves consistently across install layouts.
- `src/llm_usage/core/pricing_data/README.md` — source URL, filter recipe, schema notes (LiteLLM's per-token field names → our per-million-USD rates), provider-specific quirks for the loader.

Verified the data ships in the wheel:
```
$ uv build
$ unzip -l dist/llm_usage_mcp-0.1.0-py3-none-any.whl | grep pricing_data
   ... llm_usage/core/pricing_data/__init__.py
   ... llm_usage/core/pricing_data/README.md
   ... llm_usage/core/pricing_data/prices.json
```

#### Tests (`tests/test_prices_json.py`)
6 structural tests, all passing. Don't validate prices for correctness (rates change weekly); catch the file going corrupt, missing a v1 provider, broadening beyond v1 (drift guard), or losing the LiteLLM shape:
1. File parses; non-empty; has at least 50 models.
2. All four v1 providers present.
3. **No** unexpected providers (so a future broaden-the-filter mistake is caught).
4. Every entry has `litellm_provider` + either flat or tiered pricing fields.
5. Every entry's `mode` is `chat` or `responses`.
6. Anthropic models that advertise `supports_prompt_caching` carry **both** `cache_creation_input_token_cost` and `cache_read_input_token_cost`.

#### Verification
```bash
uv run ruff check .       # All checks passed!
uv run ruff format --check .
uv run mypy               # Success: no issues found in 13 source files
uv run pytest -q          # 47 passed
```

#### Open issues / follow-ups
- Pricing loader: parse `prices.json`, convert per-token to per-million-USD, handle tiered pricing's first tier, upsert to `pricing_snapshot`. Skip entries without `cache_creation_input_token_cost` for `cache_write_per_million_usd` (OpenAI/DeepSeek absorb the write cost).
- GitHub Action that re-runs the `jq` filter weekly and opens a PR with the diff.

---

## 中文版本

### 2026-05-04 — 项目初始化

#### 目标
搭建一个干净的 Python 项目骨架，配套现代化工具链：**uv** 管理依赖、**ruff** 做 lint 与格式化、**mypy** 做类型检查、**pytest** 做测试。

#### 初始状态
仓库已存在：
- `.git/`（已初始化的 git 仓库）
- `.gitignore`
- `LICENSE`
- `README.md`（一行项目简介）

没有任何 Python 源码、没有 `pyproject.toml`、没有虚拟环境。

#### 步骤 1 — 用 uv 初始化项目
采用 **package** 布局（而不是平铺脚本），从一开始就得到标准的 `src/` 目录结构：

```bash
uv init --package --name llm-usage-mcp
```

生成的内容：
- `pyproject.toml` — 最小配置，含 `requires-python = ">=3.13"` 和命令行脚本 `llm-usage-mcp = "llm_usage_mcp:main"`。
- `.python-version` — 固定为 `3.13`。
- `src/llm_usage_mcp/__init__.py` — 桩函数 `main()`。

#### 步骤 2 — 添加开发依赖
将 `ruff`、`mypy`、`pytest`、`pytest-cov` 加入 `dev` 依赖组：

```bash
uv add --dev ruff mypy pytest pytest-cov
```

`uv` 自动创建 `.venv/` 并把版本固化到 `uv.lock`。最终版本：
- `ruff==0.15.12`
- `mypy==1.20.2`
- `pytest==9.0.3`
- `pytest-cov==7.1.0`

#### 步骤 3 — 在 `pyproject.toml` 中配置工具
所有工具配置集中在 `pyproject.toml`（单一可信源）。要点：

- **Ruff**
  - `line-length = 100`，`target-version = "py313"`
  - 启用规则集：`E`、`W`、`F`、`I`、`B`、`C4`、`UP`、`SIM`、`RUF`、`N`、`TID`
  - `tests/**/*.py` 允许使用 `assert`（忽略 `S101`）
  - 格式化：双引号、空格缩进

- **Mypy** — 严格模式
  - `strict = true`、`warn_return_any`、`disallow_untyped_defs` 等
  - 对 `tests.*` 放宽，允许测试中存在未标注类型的辅助函数

- **Pytest**
  - `testpaths = ["tests"]`
  - `--strict-markers`、`--strict-config`
  - `filterwarnings = ["error"]` — 警告直接失败（便于尽早发现弃用）

- **Coverage**
  - `source = ["src"]`、`branch = true`
  - 排除 `if TYPE_CHECKING:` 和 `raise NotImplementedError`

#### 步骤 4 — 第一个冒烟测试
- 创建 `tests/__init__.py` 和 `tests/test_smoke.py`，里面只有一个 `test_main_runs()` 调用 `main()`。这个测试不是为了覆盖率——而是为了验证整条工具链可以端到端跑通。

#### 步骤 5 — 验证全部通过
本地跑完整套工具链，全部绿灯：

```bash
uv run ruff check .              # All checks passed!
uv run ruff format --check .     # 3 files already formatted
uv run mypy                      # Success: no issues found in 3 source files
uv run pytest -q                 # 1 passed in 0.01s
```

#### 最终目录结构
```
llm-usage-mcp/
├── .git/
├── .gitignore
├── .python-version
├── .venv/               （由 uv 管理，已被 git 忽略）
├── LICENSE
├── README.md
├── progress.md          （本文件）
├── pyproject.toml
├── src/
│   └── llm_usage_mcp/
│       └── __init__.py
├── tests/
│   ├── __init__.py
│   └── test_smoke.py
└── uv.lock
```

#### 后续常用命令
```bash
uv sync                        # 按 uv.lock 安装/更新全部依赖
uv run ruff check . --fix      # lint 并自动修复
uv run ruff format .           # 格式化代码
uv run mypy                    # 类型检查
uv run pytest                  # 运行测试
uv run pytest --cov            # 带覆盖率运行测试
uv add <pkg>                   # 添加运行时依赖
uv add --dev <pkg>             # 添加开发依赖
```

### 2026-05-04 — 项目文档（CLAUDE.md、spec、plan、适配器参考）

#### 目标
落地项目的"Agent 上下文"文档，让未来在本仓库工作的 Claude Code 会话**马上知道要做什么、怎么做、什么不要做**。

#### 新增 / 修改内容
- **`CLAUDE.md`** — 重写为精简版（≤30 行），分五个小节：*项目是什么*、*编码规范*、*工作流规则*、*Provider 易踩坑（一行版）*、*当前焦点*。文档引用 `@docs/spec.md`、`@plan.md` 和 `@docs/Provider_Adapter_Reference.md`。
- **`docs/spec.md`** — 完整的 v1 规格：项目愿景、三层架构（捕获 / 核心+SQLite / MCP 服务器）、v1 *不做*的事、技术栈、仓库结构、数据库 schema，以及 MCP 工具 API（`record_usage`、`query_spend`、`compare_providers`、`recommend_provider`、`get_pricing`、`usage_summary`、`list_providers`）。
- **`docs/Provider_Adapter_Reference.md`** — v1 四个 Provider（Anthropic、OpenAI、Qwen/DashScope、DeepSeek）的具体请求/响应结构，包含流式 SSE 模式、缓存 token 字段路径、成本公式、pricing JSON 条目模板、适配器实现骨架以及每个 Provider 的验收清单。**从 `Providing_Adapter_Reference.md` 改名而来**（修正拼写），让 `CLAUDE.md` 和 `spec.md` 中的引用能正确解析。
- **`plan.md`** — Day 1 上午任务清单（清掉了缩进噪音）。Day 2–5 标记为 TODO（源内容在半句话处被截断，需要补全）。

#### 待解决的问题
- **Python 版本不一致**。`CLAUDE.md` 和 `spec.md` 说 *Python 3.11+*，但 `pyproject.toml` 固定为 `requires-python = ">=3.13"`，且 ruff/mypy 的 `target-version = "py313"`。选一个版本并把三处对齐。
- **`plan.md` 被截断**。Day 1 列表停在 "Write plan.md with your Day 2–5 tasks as"，Day 2–5 为空。需要补全。
- **MCP `record_usage` 工具中提到 `cache_write_tokens`** — 这其实是 Anthropic 特有概念。需在适配器参考文档中说明 OpenAI/Qwen/DeepSeek 的映射方式（已部分完成；待 `spec.md` 稳定后从 `spec.md` 互链）。

### 2026-05-06 — 用法数据库的 SQLAlchemy 模型

#### 目标
把持久层 schema 落到代码里。spec 已经定义了三张表（`usage_events`、`pricing_snapshot`、`schema_version`）和四个索引，但仓库里还没有任何对应的 Python 代码。

#### 为什么先建模型，暂不建 engine
捕获层、定价层、MCP 工具层都通过这些模型读写。用 SQLAlchemy 2.0 的类型化风格（`DeclarativeBase` + `Mapped[...]`）定义出来，模型本身**对同步/异步无感**——后续 `db.py` 加 session 工厂时再决定用同步 `sqlite3` 还是异步 `aiosqlite` 都来得及。这样这次 PR 改动更小、承诺更少。

#### 包重命名：`llm_usage_mcp` → `llm_usage`
spec 的仓库布局把 import 包定在 `src/llm_usage/`，而项目骨架最初创建的是 `src/llm_usage_mcp/`。为了**与 spec 完全对齐**：
- `git mv src/llm_usage_mcp src/llm_usage`
- 控制台脚本入口改为：`llm-usage-mcp = "llm_usage:main"`。
- uv 的 build backend 默认按*项目名*推断模块名（`llm-usage-mcp` → `llm_usage_mcp`），所以构建会失败，直到在 `pyproject.toml` 加上 `[tool.uv.build-backend] module-name = "llm_usage"` 才修复。
- 现有冒烟测试改为 `from llm_usage import main`。

发行包名（`llm-usage-mcp`）保持不变——只是 import 包的目录名变了。

#### 本次落地的内容
- **`src/llm_usage/core/db.py`** —— 三个模型，与 spec 一一对应：
  - `UsageEvent`（表 `usage_events`）—— 16 列，`id` 为主键，四个索引。
  - `PricingSnapshot` —— 复合主键 `(provider, model)`。
  - `SchemaVersion` —— 单列表；常量 `CURRENT_SCHEMA_VERSION = 1`。
- **`src/llm_usage/core/__init__.py`** —— 公开导出。
- **`tests/test_models.py`** —— 9 个测试，覆盖：表集合、列与可空性、索引名与索引列、`request_id` 的部分唯一约束、原生 INSERT 时 server-default 是否生效、复合主键、读写往返。

#### 几个值得说明的设计选择
- **`metadata` 列 → Python 属性名 `event_metadata`**。SQLAlchemy 在 `Base` 上保留了 `metadata` 名字，所以列名按 spec 仍叫 `metadata`，但 ORM 这边的属性改名为 `event_metadata` 以避开冲突。
- **Token 默认值同时设了 `default=0` 和 `server_default=text("0")`**。spec 在 SQL 层写的是 `NOT NULL DEFAULT 0`，所以即便有人**绕过 ORM 写原生 SQL** 并省略这些列，也必须能成功插入。有专门的测试覆盖这一点。
- **`request_id` 的部分唯一索引**严格对应 spec 的 `UNIQUE WHERE request_id IS NOT NULL`，通过 `Index(..., unique=True, sqlite_where=text("request_id IS NOT NULL"))` 实现。这正是**幂等记录**的关键——回放抓取的日志不会重复计数。
- **用 `Float` 而不是 `REAL`**。`sqlalchemy.Float` 是跨数据库的浮点类型，在 SQLite 上渲染成 `REAL`，存储形态完全一样，但在 SQLAlchemy 2.0 里更地道。
- **暂不创建 engine 与 session 工厂**。等到 `~/.llm-usage/usage.db` 路径解析与异步方案一起落地时再做。

#### 验证
```bash
uv run ruff check .       # All checks passed!
uv run ruff format --check .
uv run mypy               # Success: no issues found in 6 source files
uv run pytest -q          # 9 passed
```

#### 待办 / 后续
- Engine 与 session 工厂（`get_engine()`、`SessionLocal`）—— 下一个 PR。
- Alembic 初始化以及第一版 revision，让 schema 升级和 `schema_version` 一起被纳入版本控制。
- 给 `UsageEvent` 配一个 Pydantic 镜像类型，供 MCP `record_usage` 工具的入参/出参使用（spec 中 MCP 层都用 Pydantic）。

### 2026-05-06 — Alembic 初始化与首次迁移

#### 目标
把 Alembic 接进来，让 schema 后续可以在版本控制下演进，并把当前的模型定格为 `278ba38a2efd`（`initial schema`）。

#### 为什么先做 Alembic、再做 engine 工厂
上一个 PR 把模型敲下来了，但还没有任何代码把它真正落到磁盘上。**先接 Alembic** 意味着第一次 `alembic upgrade head` 跑出的磁盘 schema 就已经和 `Base.metadata` 完全一致——这样 `Base.metadata.create_all()` 和迁移脚本就不会有走样的机会。

#### 目录布局
- `alembic.ini` 放在仓库根目录（标准脚手架）。
- `alembic/env.py` 改成从 `llm_usage.core` 导入 `Base.metadata`，并对 SQLite 启用 `render_as_batch=True`（这是后续在 SQLite 上做 ALTER TABLE 迁移所必需的，必须走 Alembic 的 batch 模式）。
- `alembic/versions/278ba38a2efd_initial_schema.py` —— 自动生成、复核后稍作修改的首版 revision。

#### 数据库 URL 的解析方式
`alembic.ini` 中的 `sqlalchemy.url` 留空。`env.py` 在运行时按下面的优先级解析：
1. 若设置了 `$LLM_USAGE_DB_URL` 就用它（测试与 CI 用）。
2. 否则按 spec 默认到 `sqlite:///~/.llm-usage/usage.db`。

这样既把 spec 默认值放进代码（最终用户直接 `alembic upgrade head` 就能跑通），又避免把环境相关的路径写死到 `alembic.ini` 里。

#### 首版迁移是怎么生成的
```bash
LLM_USAGE_DB_URL='sqlite:///:memory:' uv run alembic revision --autogenerate -m "initial schema"
```
指向 `:memory:` 的好处是给 autogenerate 一个**完全空的**数据库做对比，这样它会把全套 `CREATE TABLE` 都吐出来，而不是和 `~/.llm-usage/usage.db` 里碰巧的内容做差。

#### autogenerate 一次到位（无需手改）的部分
- 三张表的列类型和可空性都对。
- `pricing_snapshot (provider, model)` 复合主键。
- token 列默认 `0`，`success` 默认 `1`，server_default 都正确。
- 四个索引——尤其是 **部分唯一索引** `idx_events_request_id`，`sqlite_where=sa.text("request_id IS NOT NULL")` 也保留下来了。这正是让记录幂等的关键；迁移测试专门断言 DDL 中的 `WHERE` 子句没丢。

#### 唯一一处手工补充：`schema_version` 种子数据
Alembic 自己维护版本表（`alembic_version`），spec 里的 `schema_version` 是另一张面向用户/工具的"schema 大版本号"表，不依赖 Alembic 也能被读取。所以首版迁移末尾加了一行：
```python
op.execute(sa.text("INSERT INTO schema_version (version) VALUES (1)"))
```
两张表是**有意并存**的——它们回答的不是同一个问题。

#### Post-write hooks
`alembic.ini` 配置了在每次新生成 revision 文件之后自动跑 `ruff check --fix` 与 `ruff format`。以后 `alembic revision --autogenerate` 出来的脚本天然 lint 干净，不用手动收拾。

#### 测试（`tests/test_migrations.py`）
5 个测试全部通过：
1. `upgrade head` 后能拿到 spec 中的全部表（再加一张 `alembic_version`）。
2. 模型上的列与磁盘上的列一一对应（防止两边走样）。
3. `sqlite_master` 中 `WHERE request_id IS NOT NULL` 子句仍然存在。
4. `schema_version` 被种入 `1`。
5. `downgrade base` 能干净地清除全部 spec 表。

#### 验证
```bash
uv run ruff check .       # All checks passed!
uv run ruff format --check .
uv run mypy               # Success: no issues found in 7 source files
uv run pytest -q          # 14 passed
```

#### 待办 / 后续
- engine 与 session 工厂，以及一个和 `env.py` 解析方式一致的 `db_path()` 助手（让应用代码和 Alembic 用同一个文件）。
- 一个 `llm-usage-mcp db upgrade` 子命令，对终端用户封装 `alembic upgrade head`。
- 在首次启动时把内置 `prices.json` 加载进 `pricing_snapshot` 的 loader。

### 2026-05-06 — Engine 与 session 工厂（同步），与 Alembic 共用 URL

#### 目标
把持久层的运行时部分落到代码里：engine 工厂、进程级 session、以及 SQLite WAL 调优。**只做同步**——异步等到 FastAPI capture 代理真的开始动工再说。

#### 为什么先做同步
SQLite 是单写者数据库，async 并不能让写并行起来；它真正的价值是"别堵住 event loop"。当前的调用方（MCP 工具、冒烟测试）都不在 event loop 上，FastAPI 代理在 spec 里有但还没建。同步代码在写、测、调试上都更简单；SQLAlchemy 2.0 后续要加 `AsyncSession` 完全可以**复用同一套模型和同一个 URL**，不需要重写模型。等代理真正落地时，正确的做法是"在同步旁边再加一套异步"——先做同步并不会把自己堵死。

#### 目录调整：`core/db.py` → `core/db/` 包
- `core/db/models.py` —— 从原 `core/db.py` 原样搬过来。
- `core/db/session.py` —— 新增：`resolve_db_url`、`create_engine`、`get_engine`、`get_session_factory`、`get_session`、`DEFAULT_DB_PATH`。
- `core/db/__init__.py` —— 把两个模块的公开 API 都重新导出，所以现有 `from llm_usage.core import ...` 用法保持不变。

spec 把模型和 session 都塞在一个 `db.py` 里；我们拆开是因为 `db.py` 已经 100 行打底了，再塞 engine 相关代码就会撑得不太舒服。对外 import 路径 (`from llm_usage.core import ...`) 完全不变。

#### `session.py` 提供了什么
- **`resolve_db_url()`** —— 先看环境变量 `LLM_USAGE_DB_URL`，否则按 spec 默认到 `sqlite:///~/.llm-usage/usage.db`。**现在和 `alembic/env.py` 共用同一个函数**，保证应用代码和迁移指向同一个数据库；`env.py` 里那份重复实现已经删掉。
- **`create_engine(url=None)`** —— 工厂函数。对于文件型 SQLite URL，会确保父目录存在（这样在干净机器上首次运行不会因为目录不在而崩），并注册 `connect` 监听器。
- **WAL pragma 监听器** —— 每次 connect 到文件型 SQLite engine 时，跑 `PRAGMA journal_mode=WAL` 和 `PRAGMA synchronous=NORMAL`。这是标准搭配：WAL 让写期间还能并发读；NORMAL 跳过每次 commit 的 fsync（fsync 在 WAL checkpoint 时仍会发生），对本地开发工具来说是耐久性和性能的甜点。In-memory 和非 SQLite URL 都会跳过这两步。
- **`get_engine()` / `get_session_factory()` / `get_session()`** —— 模块级懒加载单例。首次调用构建，后续复用。

#### 测试（`tests/test_session.py`）
6 个测试全部通过：
1. `resolve_db_url` 默认路径正确。
2. `resolve_db_url` 能被环境变量覆盖。
3. `create_engine` 在嵌套路径下会自动创建父目录。
4. WAL 模式与 `synchronous=NORMAL` 在 connect 时确实生效（断言读 `PRAGMA journal_mode` 和 `PRAGMA synchronous`）。
5. In-memory URL（`sqlite:///:memory:`）不会上 WAL —— `journal_mode` 报的是 `memory` 而不是 `wal`。
6. **端到端冒烟**：插入一条 `UsageEvent`，再查回来，断言读出来的字段和写进去的一致。

#### 一个值得记的测试细节
冒烟测试要走懒加载单例（`get_engine`、`get_session_factory`）这条路径。直接走的话，全局 SQLite 连接池会跨测试存活，被 GC 回收时会触发 `PytestUnraisableExceptionWarning`，而 `filterwarnings = ["error"]` 会把这个警告升级成致命错误。所以专门写了一个 `reset_session_singletons` fixture，在每个测试前后把模块级单例置空并 dispose。

#### 验证
```bash
uv run ruff check .       # All checks passed!
uv run ruff format --check .
uv run mypy               # Success: no issues found in 10 source files
uv run pytest -q          # 20 passed（9 model + 5 migration + 6 session）
```

#### 待办 / 后续
- 一个 `llm-usage-mcp db upgrade` 子命令，封装 `alembic upgrade head`；外加一个保证 `~/.llm-usage/` 在首次迁移前存在的 `init` 步骤。
- Pricing 数据加载器：把内置的 `prices.json` 读出来 upsert 到 `pricing_snapshot`。
- MCP 工具边界用的 Pydantic 模型（`record_usage` 的入参/出参等）。

### 2026-05-06 — Pricing + CostCalculator（顺带改了 spec：`cost_usd` → `cost_nano_usd`）

#### 先说 spec 改动
原本 spec 里 `usage_events` 的成本列是 `cost_usd REAL NOT NULL`。在设计 calculator 时把它改成了 `cost_nano_usd INTEGER NOT NULL`（10⁻⁹ USD），并同步更新了 `docs/spec.md`。原因：

- **聚合算术精确**。`SUM(cost_nano_usd)` 在百万级行上是整数加法，精确无误；浮点 `SUM` 会累积误差。
- **分辨率够用**。Anthropic 1 个 cache_read token 在 $0.30/M 下约等于 $3 × 10⁻⁷ = 300 nano-USD，干净可表示。如果用"分"做单位反而会被 round 成 0。
- **数值上限充足**。SQLite INTEGER 是 64 位有符号 → 最大约 $9.2 B，远超任何用户的开销。
- **业内惯例**。Stripe 和支付系统都把钱以"最小货币单位"的整数存。nano-USD 只是把单位调小到能表达 LLM 的微观计费。

MCP 工具边界对外仍然返回 float `cost_usd: number`，到边界处一句 `cost_nano_usd / 1e9` 完成换算 —— 终端用户看到的还是好懂的 USD，存储层则是精确的整数。

#### 迁移：`0002_cost_usd_to_cost_nano_usd`
SQLite 不能直接 `ALTER COLUMN` 改类型，所以用三个 batch_alter 阶段，让已有数据安全过渡：

1. 加 `cost_nano_usd INTEGER`（nullable，先不带默认）。
2. `UPDATE usage_events SET cost_nano_usd = CAST(cost_usd * 1000000000 AS INTEGER)`。
3. 把 `cost_nano_usd` 收紧到 `NOT NULL`，删掉 `cost_usd`。

downgrade 对称反向（`cost_nano_usd / 1000000000.0`）。手动验证过：一行 `cost_usd = 0.0042` 升级后 `cost_nano_usd = 4_200_000`，下迁回去精确还原。

#### `core/pricing.py`
- **`Pricing`** —— 冻结 dataclass。费率仍然用 `float`（每百万 token USD 经常带小数，例如 $3.75、$0.30）；`fetched_at` 也带过来，方便回头查"为什么今天的成本和昨天不一样？"。`Pricing.from_orm(PricingSnapshot)` 负责 ORM→dataclass 的转换。
- **`CostCalculator`** —— 绑一个 `Pricing` 实例。唯一对外方法是 `cost_nano_usd(*, input_tokens, output_tokens, cache_write_tokens=0, cache_read_tokens=0) -> int`。算式就是 `tokens × rate × 1_000`（rate 是每百万 USD，×1000 把它换成"每 token nano-USD"），四种 token 加起来，最后做银行家舍入到整数。
- **`get_pricing(session, provider, model) -> Pricing | None`** —— 从 `pricing_snapshot` 里取一行。找不到时返回 `None`（后续 MCP 层 `record_usage` 会按 spec 把它翻译成 `warning` 字段）。

#### 校验：cache 费率缺失时严格报错
如果 `cache_write_tokens > 0` 而 `cache_write_per_million_usd` 是 `None`（cache_read 同理），`cost_nano_usd` 会抛 `ValueError`。原因：这种情况要么是 pricing 数据过期、要么是 adapter 写错了 —— 静默地把这部分 cost 当成 0 只会让账少算而完全不报警。错误消息会带上 provider/model，光看 stack trace 就能定位数据问题。`cache_*_tokens = 0` 配 `None` 费率是合法的 —— cache 那部分贡献就是 0。

负数 token 也会报错；我们信任调用方，但不至于让一笔"反向退款"悄悄通过。

#### 测试数据：用真实 Anthropic 费率
测试里直接用 Claude Sonnet 4.6 的真实费率（$3 / $15 / $3.75 / $0.30）。这样测试同时也是活文档：读测试就能看到 cache 价格的实际量级（cache_read 是 input 的 0.10×，cache_write 是 input 的 1.25×）。组合场景测试就是我们解释 caching 时用的那个例子：100K cache_write + 900K cache_read + 1.5K input/output ≈ $0.6555 = 655_500_000 nano-USD。

#### 验证
```bash
uv run ruff check .       # All checks passed!
uv run ruff format --check .
uv run mypy               # Success: no issues found in 12 source files
uv run pytest -q          # 41 passed（9 model + 5 migration + 6 session + 21 pricing）
```

#### 待办 / 后续
- Pricing 数据加载器：读取内置 `prices.json` 并 upsert 到 `pricing_snapshot`（按 `(provider, model)` 主键幂等；每次刷新更新 `fetched_at`）。
- `record_usage` MCP 工具：把 token 数 → `get_pricing` → `CostCalculator` → 插入 `UsageEvent` 串起来。pricing 缺失时按 spec 写入 `warning` 字段。
- `llm-usage-mcp db upgrade` 子命令。

### 2026-05-07 — 把 LiteLLM 的定价 JSON 内置进来（裁剪后）

#### 目标
落地"定价数据的源头"：`src/llm_usage/core/pricing_data/prices.json`。后续的 loader 可以从这里读出来灌进 `pricing_snapshot`。

#### 来源
LiteLLM 的 [`model_prices_and_context_window_backup.json`](https://raw.githubusercontent.com/BerriAI/litellm/main/litellm/model_prices_and_context_window_backup.json) —— LiteLLM 自己内部做成本追踪用的也是这个文件。spec 早就钦点了这个约定（"按 LiteLLM 的 `model_prices.json` 模式来"）。每周刷新一次（暂时手动；GitHub Action 排在路线图里）。

#### 为什么原样保留 LiteLLM 的字段结构
刷新工序就是一行命令（下载 → `jq` 过滤 → 提交）。等到要往 `pricing_snapshot` 里灌的时候，由 loader 在加载时做一次字段换算就行；提交进来的 JSON 仍然兼容任何其他解析 LiteLLM 数据的工具。换算成本只付一次，而不是每次刷新都付。

#### 应用的过滤规则
```jq
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
```

- **Provider**：anthropic、openai、deepseek、dashscope（阿里云的 API，也是 Qwen 在 v1 走的路径）。LiteLLM 还有 Anthropic via Bedrock、via Vertex 等，但 v1 范围只包含 Provider 直接 API，那些先排除。
- **Mode**：`chat` 和 `responses`（OpenAI 的 Responses API）。Embeddings、音频、图像、moderation、视频全部排除——它们的定价不是"每百万 token"形式。
- **完全没有定价的条目**：丢掉了两条只有元数据没有费率的（`dashscope/qwen3-30b-a3b`、`openai/container`）。LiteLLM 留着这些做参考，但 loader 反正也定不了价。

最终：**178 个模型**，约 150 KB。各家分布：anthropic 20、dashscope 32、deepseek 8、openai 118。

#### 阶梯定价的小怪
有几款 Qwen 模型用的是 `tiered_pricing`（一个 `{range, input_cost_per_token, output_cost_per_token}` 列表）而不是平直费率——阿里在不同 token 数阈值之上收不同价。结构性测试两种格式都允许；loader 后续要从中挑一档基准价（大概率取第一档）灌进 `pricing_snapshot`。Anthropic 的 `*_above_200k_tokens` 也是类似的阶梯，但它跟平直费率一起出现，处理起来简单得多（直接用平直那档，阶梯的字段忽略）。

#### 文件
- `src/llm_usage/core/pricing_data/prices.json` —— 裁剪后的载荷。
- `src/llm_usage/core/pricing_data/__init__.py` —— 空文件；让目录变成正式 package，`importlib.resources.files(...)` 在不同安装布局下都能稳定解析到。
- `src/llm_usage/core/pricing_data/README.md` —— 源地址、过滤命令、字段含义说明（LiteLLM 的 per-token → 我们的 per-million-USD）、Provider 各自的小坑给 loader 看。

确认数据真的随 wheel 一起发布：
```
$ uv build
$ unzip -l dist/llm_usage_mcp-0.1.0-py3-none-any.whl | grep pricing_data
   ... llm_usage/core/pricing_data/__init__.py
   ... llm_usage/core/pricing_data/README.md
   ... llm_usage/core/pricing_data/prices.json
```

#### 测试（`tests/test_prices_json.py`）
6 个结构性测试，全部通过。**不**校验价格是否准确（每周变动），只防三件事：文件损坏、漏了 v1 Provider、过滤范围被无意中放宽（drift 守门员），以及 LiteLLM 字段结构变了：
1. 文件能被解析；非空；至少 50 个模型。
2. 四家 v1 Provider 都在。
3. **不出现**额外 Provider（防止后续有人不小心放宽过滤）。
4. 每个条目都有 `litellm_provider`，并且有平直或阶梯定价之一。
5. 每个条目的 `mode` 都是 `chat` 或 `responses`。
6. 凡是 Anthropic 中标了 `supports_prompt_caching` 的模型，都同时有 `cache_creation_input_token_cost` 和 `cache_read_input_token_cost`。

#### 验证
```bash
uv run ruff check .       # All checks passed!
uv run ruff format --check .
uv run mypy               # Success: no issues found in 13 source files
uv run pytest -q          # 47 passed
```

#### 待办 / 后续
- Pricing loader：解析 `prices.json`、把 per-token 换成 per-million-USD、处理阶梯定价取第一档、upsert 到 `pricing_snapshot`。如果某条目没有 `cache_creation_input_token_cost`，`cache_write_per_million_usd` 设成 `None`（OpenAI / DeepSeek 把 cache write 成本吃了）。
- GitHub Action：每周自动重跑 `jq` 过滤，把 diff 提一个 PR。
