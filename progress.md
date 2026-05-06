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
