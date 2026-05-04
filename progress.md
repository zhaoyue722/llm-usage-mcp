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
