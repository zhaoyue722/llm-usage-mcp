# llm-usage-mcp

> [English](README.md) | 中文

一个**本地优先、多家厂商通用**的 LLM 用量记录工具：把每一次 LLM API 调用记到本地 SQLite，再通过 [Model Context Protocol (MCP)](https://modelcontextprotocol.io) 暴露给任意编码 Agent 查询。

[![CI](https://github.com/zhaoyue722/llm-usage-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/zhaoyue722/llm-usage-mcp/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.13+](https://img.shields.io/badge/python-3.13%2B-blue.svg)](https://www.python.org/downloads/)

## 为什么需要它

一个编码 Agent 今天会同时调用好几家的模型 —— Anthropic Claude、OpenAI GPT、阿里通义千问（Qwen）、DeepSeek。账单分散在不同的控制台，计价货币不同（USD / CNY），缓存定价的口径也各不相同：

- Anthropic 的 cache write 是输入价的 1.25 倍，cache read 是 0.1 倍；
- OpenAI 的 `prompt_tokens_details.cached_tokens` 嵌套在 usage 里；
- DeepSeek 用 `prompt_cache_hit_tokens` / `prompt_cache_miss_tokens` 切分；
- Qwen 在 OpenAI 兼容端点上通常根本不返回缓存字段。

`llm-usage-mcp` 做的事情很简单：**把所有调用统一记到本地 `~/.llm-usage/usage.db` 一张表里**，然后通过 MCP 工具让 Claude Code、Cursor 这类客户端能直接回答「这周在 Claude 上花了多少」「跑这个 10k 输入 / 2k 输出的活儿哪家最便宜」「给我一个 $0.01 预算内的推荐模型」。

特点：

- **本地优先**：没有 SaaS、不需要注册、不上传任何数据。SQLite 文件就在 `~/.llm-usage/usage.db`，可以随时 `sqlite3` 进去翻。隐私本身就是产品的一部分。
- **多厂商**：v1 已支持 Anthropic、OpenAI、DeepSeek、Qwen 四家，**流式和非流式都覆盖**。
- **国产模型友好**：DeepSeek 和 Qwen（DashScope OpenAI 兼容端点）是一等公民，跟海外厂商走同一条捕获链路，不是事后补丁。
- **MCP 原生**：读写都走 MCP，所以任何支持 MCP 的客户端（Claude Code、Cursor、自研 Agent…）拿到的是同一套接口。

## 两分钟跑起来

从 `git clone` 到「Claude Code 能告诉我刚才花了多少钱」，大概两分钟。

### 1. 安装

```bash
git clone https://github.com/zhaoyue722/llm-usage-mcp.git
cd llm-usage-mcp
uv sync
```

`uv sync` 会安装项目和开发依赖，并在 venv 里建好两个命令：

- `llm-usage-proxy` —— 抓取代理（Layer 1）。
- `llm-usage-mcp` —— MCP 服务器（Layer 3）。

> 如果你还没装 [uv](https://docs.astral.sh/uv/)，国内推荐用 `pip install uv` 或者镜像源装一次。Python 要求 3.13+。

### 2. 至少配一个厂商的 API Key

只需要给**你实际会用**的厂商配 key，proxy 启动时不会因为别家没配就拒绝启动 —— 没配的路由会单独返回 `503 configuration_error`。

```bash
# 海外（需要能访问境外网络）
export ANTHROPIC_API_KEY=sk-ant-...
export OPENAI_API_KEY=sk-...

# 国内（直连即可）
export DEEPSEEK_API_KEY=sk-...
export DASHSCOPE_API_KEY=sk-...   # 通义千问，从阿里云 DashScope 控制台拿
```

> **网络环境提示**：Anthropic 和 OpenAI 的官方端点在国内通常需要走海外网络。如果你用第三方反代或自建网关，把对应的 `*_BASE_URL` 环境变量指过去即可（见 [`docs/configuration.md`](docs/configuration.md)）。DeepSeek 和 DashScope（Qwen）走的是国内公网，直连没问题。

完整环境变量参考：[`docs/configuration.md`](docs/configuration.md)，也可以把 [`.env.example`](.env.example) 复制成 `.env` 再填。

### 3. 启动抓取代理

```bash
uv run llm-usage-proxy
```

代理**只绑定 loopback**（`127.0.0.1:5525`），局域网里其他机器连不上 —— 这是写死的，不是配置项，因为「本地优先 + 隐私是产品的一部分」不能容忍一次配置失误就把代理暴露到公网。API Key 由 proxy 持有，客户端那一侧不需要任何 key。

### 4. 把编码 Agent 的请求指向代理

每个厂商一条路由。在客户端这一侧设置对应的 `*_BASE_URL`：

| 厂商 | 客户端环境变量 | 设置值 |
|---|---|---|
| Anthropic | `ANTHROPIC_BASE_URL` | `http://127.0.0.1:5525` |
| OpenAI | `OPENAI_BASE_URL` | `http://127.0.0.1:5525/openai/v1` |
| DeepSeek | `DEEPSEEK_BASE_URL`（或任何 OpenAI SDK 的 base-url 覆盖） | `http://127.0.0.1:5525/deepseek/v1` |
| Qwen | DashScope 的 OpenAI 兼容 base | `http://127.0.0.1:5525/qwen/v1` |

例子 —— 让 Claude Code 的调用全部走代理：

```bash
ANTHROPIC_BASE_URL=http://127.0.0.1:5525 claude
```

例子 —— 用 OpenAI SDK 调通义千问，同时被记下来：

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://127.0.0.1:5525/qwen/v1",
    api_key="not-needed",  # 真 key 在 proxy 那一侧
)
resp = client.chat.completions.create(
    model="qwen-flash",
    messages=[{"role": "user", "content": "你好"}],
)
```

每一次调用都会带着 token 数、成本（精确到纳美元）、延迟、`request_id`（去重用）一起落到 `~/.llm-usage/usage.db`。

### 5. 通过 MCP 查询用量

把 MCP 服务器挂到 Claude Code：

```bash
claude mcp add llm-usage -- uv --directory $(pwd) run llm-usage-mcp
```

然后在 Claude 里直接问：

> 今天我在 Anthropic 上花了多少？跑一次 10k 输入 / 2k 输出的活儿，哪家最便宜？

Claude 会在背后调用 `usage_summary`、`query_spend`、`compare_providers` 之类的工具。

## MCP 工具一览

七个工具加两个资源，通过 stdio 暴露。完整参数 / 返回值见 [`docs/spec.md`](docs/spec.md)。

| 工具 | 作用 |
|---|---|
| `query_spend` | 在一个时间窗口内统计总花费，可按 provider / model / project / tag / day 分组。 |
| `usage_summary` | 「今天 / 本周 / 本月 / 今年」一句话总结：总花费、Top-3 厂商、Top-3 模型、最贵的一次调用。 |
| `compare_providers` | 给定一个假设的工作量（输入 / 输出 token 数），把所有已知模型按成本排序。 |
| `recommend_provider` | 在预算之内挑最便宜的模型。 |
| `get_pricing` | 直接查当前的定价快照。 |
| `list_providers` | 列出所有已知厂商、它们的模型、是否 OpenAI 兼容。 |
| `record_usage` | 手动写入一条调用记录 —— 当 proxy 不在链路上时（例如离线分析、批量补录）。 |

`query_spend` 和 `usage_summary` 默认 `include_failed=false`，所以流式中断写下的部分计数行不会污染总额；想看可以显式传 `true`。

## 架构

```
Layer 3:  src/llm_usage/mcp/       —— MCP 工具 + 资源（读路径）
Layer 2:  src/llm_usage/core/      —— SQLite + 定价 + 成本计算
Layer 1:  src/llm_usage/capture/   —— 抓取代理：Anthropic / OpenAI / DeepSeek / Qwen
```

中间一张 SQLite（`~/.llm-usage/usage.db`）。Proxy 和 MCP 服务器是**两个独立进程**，没有 IPC，只是恰好指向同一个文件。Cost 在**写入时**就按当时的定价快照计算好，所以以后改价不会回写历史 —— 这是事件溯源的标准做法。

流式抓取的实现是「**SSE 字节透传给客户端不变，旁路 parser 累积 usage 块**」：Anthropic 走 `message_start` + `message_delta` 两段 usage，OpenAI 家族走最后一个带 `usage` 的 chunk（`stream_options.include_usage=true`）。两条路径的细节都写在 [`docs/architecture.md`](docs/architecture.md) 里。

## 支持的厂商（v1）

| 厂商 | 认证 | 非流式 | 流式 | 缓存计价 |
|---|---|---|---|---|
| Anthropic | `x-api-key` | 是 | 是 | `cache_creation` + `cache_read` |
| OpenAI | `Bearer` | 是 | 是 | 嵌套的 `prompt_tokens_details.cached_tokens` |
| DeepSeek | `Bearer` | 是 | 是 | `prompt_cache_hit_tokens` / `_miss_tokens` |
| Qwen (DashScope) | `Bearer` | 是 | 是 | OpenAI 兼容端点上通常不返回 |

定价数据是 [LiteLLM 定价 JSON](https://github.com/BerriAI/litellm/blob/main/litellm/model_prices_and_context_window_backup.json) 的精简快照，通过 GitHub Action 每周自动刷新（[`refresh-pricing.yml`](.github/workflows/refresh-pricing.yml)）。

LiteLLM 暂时还没收录的型号（比如 2026-05 之后才上的 `deepseek-v4-flash`）我们用 [`pricing_overrides.json`](src/llm_usage/core/pricing_data/pricing_overrides.json) 在本地兜底，每次重启 proxy / MCP 服务器都会重新合并 —— 编辑完那个文件再重启就生效。

## 配置

所有配置都走环境变量（或仓库根目录的 `.env`）。默认值已经够用，启动 proxy 不强制要求任何配置项。完整参考见 [`docs/configuration.md`](docs/configuration.md)。常用的几个：

| 变量 | 默认值 | 作用 |
|---|---|---|
| `LLM_USAGE_DB_URL` | `sqlite:///$HOME/.llm-usage/usage.db` | 本地数据库路径。 |
| `LLM_USAGE_PROXY_PORT` | `5525` | Proxy 端口（永远是 loopback）。 |
| `LLM_USAGE_ANTHROPIC_BASE_URL` | `https://api.anthropic.com` | 用第三方反代时改这里。 |
| `LLM_USAGE_OPENAI_BASE_URL` | `https://api.openai.com/v1` | 同上。 |
| `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / `DEEPSEEK_API_KEY` / `DASHSCOPE_API_KEY` | 未设置 | 各厂商 key，按需配置即可。 |

## 开发

```bash
uv sync                                  # 安装项目和开发依赖
uv run pytest                            # 392 个测试，约 3s
uv run ruff check src/ tests/            # lint
uv run ruff format --check src/ tests/   # 格式检查
uv run mypy                              # --strict
```

CI（[`.github/workflows/ci.yml`](.github/workflows/ci.yml)）在每个 PR 和 main 上都跑这四步，并设了 80% 的覆盖率底线（实际 ~93%）。

## 想加新厂商？

参见 [`docs/post_v1_providers.md`](docs/post_v1_providers.md)（英文），里面列了 Google Gemini、AWS Bedrock、Moonshot、Zhipu GLM、MiniMax、文心一言等厂商接入的预估工作量和坑。每加一个厂商有两笔账要算：

1. **定价数据**：在 `prices.json` / `pricing_overrides.json` 加几行，小时级别。
2. **抓取适配器**：解析它特有的 usage 形状（尤其流式），天到周级别。

两者**可以分开做** —— 先把定价加进来用 `record_usage` 手工记，等需求大了再写适配器，是合理的中间态。

## 许可证

[MIT](LICENSE).
