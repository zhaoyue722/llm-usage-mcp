# llm-usage-mcp

> [English](README.md) | 中文

一个本地优先、不挑厂商的 LLM 用量记账工具：把你每一次 API 调用都记到本地一个 SQLite 文件里，再通过 [Model Context Protocol (MCP)](https://modelcontextprotocol.io) 交给编码 Agent 去查。

[![CI](https://github.com/zhaoyue722/llm-usage-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/zhaoyue722/llm-usage-mcp/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.13+](https://img.shields.io/badge/python-3.13%2B-blue.svg)](https://www.python.org/downloads/)

## 为什么要做这个

现在一个编码 Agent 一天下来，可能同时调了好几家的模型——Anthropic 的 Claude、OpenAI 的 GPT、阿里的通义千问、DeepSeek。问题随之而来：账单散在四个控制台，货币不统一(有的算美元有的算人民币)，连「缓存」这件事每家的算法都不一样。

光是缓存计价，四家就是四个口径：

- **Anthropic**：写缓存按输入价的 1.25 倍收，读缓存按 0.1 倍——便宜，但得分开算；
- **OpenAI**：缓存命中数藏在 `usage.prompt_tokens_details.cached_tokens` 里；
- **DeepSeek**：干脆把输入拆成 `prompt_cache_hit_tokens` 和 `prompt_cache_miss_tokens` 两栏；
- **Qwen**：在 OpenAI 兼容端点上，缓存字段经常压根不返回。

`llm-usage-mcp` 把这摊事收拢成一句话能回答的东西：**所有调用统一落到本地 `~/.llm-usage/usage.db` 的一张表里**，成本在写入那一刻就按当时的价格算好。然后通过 MCP 暴露出去，Claude Code、Cursor 这些客户端就能直接回答：

> 这周在 Claude 上花了多少？
> 跑这个 10k 输入 / 2k 输出的活儿，哪家最便宜？
> 给我推荐一个一分钱以内的模型。

几个不打算妥协的点：

- **本地优先**。没有 SaaS，不用注册，不上报任何数据。数据库就是你硬盘上的一个 SQLite 文件，想看随时 `sqlite3` 进去翻。隐私不是卖点，是默认。
- **多厂商，一视同仁**。v1 支持 Anthropic、OpenAI、DeepSeek、Qwen 四家，流式和非流式全覆盖。国产模型(DeepSeek、Qwen)走的是和海外厂商完全相同的捕获链路——一等公民，不是事后打的补丁。
- **MCP 原生**。读和写都走 MCP，所以换客户端不用换接口：Claude Code、Cursor、你自己写的 Agent，拿到的是同一套工具。

## 两分钟跑起来

从 `git clone` 到「Claude Code 能告诉我刚才花了多少」，差不多两分钟。

### 1. 安装

```bash
git clone https://github.com/zhaoyue722/llm-usage-mcp.git
cd llm-usage-mcp
uv sync
```

`uv sync` 装好项目和开发依赖，顺手在 venv 里建三个命令：

- `llm-usage` —— 主 CLI，七个子命令，详见下面的 [命令行工具](#命令行工具)。
- `llm-usage-mcp` —— stdio 模式的 MCP 服务器(Layer 3)。
- `llm-usage-proxy` —— 抓取代理(Layer 1)；其实就是 `llm-usage proxy` 的别名，留着是为了向后兼容。

> 还没装 [uv](https://docs.astral.sh/uv/) 的话，`pip install uv` 一次就够，国内建议配个镜像源。Python 需要 3.13+。

### 2. 至少配一个厂商的 Key

只给**你真正会用**的厂商配 key 就行。proxy 不会因为某家没配就罢工——没配的那条路由会单独返回 `503 configuration_error`，其他照常。

```bash
# 海外(需要能访问境外网络)
export ANTHROPIC_API_KEY=sk-ant-...
export OPENAI_API_KEY=sk-...

# 国内(直连就行)
export DEEPSEEK_API_KEY=sk-...
export DASHSCOPE_API_KEY=sk-...   # 通义千问，在阿里云 DashScope 控制台拿
```

> **关于网络**：Anthropic 和 OpenAI 的官方端点在国内一般得走境外网络。如果你用第三方反代或自建网关，把对应的 `*_BASE_URL` 指过去即可(见 [`docs/configuration.md`](docs/configuration.md))。DeepSeek 和 DashScope 走国内公网，直连无碍。

完整环境变量清单在 [`docs/configuration.md`](docs/configuration.md)；或者把 [`.env.example`](.env.example) 复制成 `.env` 填一填。

### 3. 启动抓取代理

```bash
uv run llm-usage-proxy
```

代理**只绑 loopback**(`127.0.0.1:5525`)，同一个局域网里别的机器也连不上。这是写死在代码里的，不是配置项——「本地优先 + 隐私默认」这种话，经不起一次手滑就把代理暴露到公网。你的 API Key 由 proxy 这一侧保管，客户端那头一个 key 都不用碰。

### 4. 把 Agent 的请求指向代理

一个厂商一条路由，在客户端这一侧设对应的 `*_BASE_URL`：

| 厂商 | 客户端环境变量 | 值 |
|---|---|---|
| Anthropic | `ANTHROPIC_BASE_URL` | `http://127.0.0.1:5525` |
| OpenAI | `OPENAI_BASE_URL` | `http://127.0.0.1:5525/openai/v1` |
| DeepSeek | `DEEPSEEK_BASE_URL`(或任何 OpenAI SDK 的 base-url 覆盖) | `http://127.0.0.1:5525/deepseek/v1` |
| Qwen | DashScope 的 OpenAI 兼容 base | `http://127.0.0.1:5525/qwen/v1` |

比如，让 Claude Code 的所有调用都过代理：

```bash
ANTHROPIC_BASE_URL=http://127.0.0.1:5525 claude
```

或者用 OpenAI SDK 调通义千问，同样会被记下来：

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

每一次调用都会带着 token 数、成本(精确到纳美元)、延迟、还有一个用来去重的 `request_id`，一起落进 `~/.llm-usage/usage.db`。

### 5. 通过 MCP 查用量

把 MCP 服务器挂到 Claude Code：

```bash
claude mcp add llm-usage -- uv --directory $(pwd) run llm-usage-mcp
```

然后在那个会话里直接问：

> 今天我在 Anthropic 上花了多少？跑一次 10k 输入 / 2k 输出，哪家最便宜？

Claude 会在背后帮你调 `usage_summary`、`query_spend`、`compare_providers` 这些工具，你只管看答案。

## 命令行工具

MCP 工具的命令行镜像——同样的七件事，塞进一个 `llm-usage` 命令底下。有时候自己敲一行比开口问 Agent 更快。

```text
$ llm-usage
 Local-first LLM spend capture + query, exposed over MCP.

 Commands
   proxy      启动本地抓取代理(127.0.0.1)。
   compare    把一个假想工作量的成本投影到每一个有定价的模型上。
   models     翻一翻本地的定价表。
   recommend  按工作量 + 预算挑一个最便宜的模型。
   spend      按自然时段看已记录的花费。
   status     本地安装的体检表：数据库、代理、厂商、定价。
   providers  列出已配置的厂商：key 状态、协议格式、模型数。
```

| 命令 | 回答的问题 |
|---|---|
| [`compare`](#compare) | 给定一个工作量，谁最便宜？ |
| [`models`](#models) | 每百万 token 到底各家收多少？ |
| [`recommend`](#recommend) | 别让我选了，直接给我挑一个。 |
| [`spend`](#spend) | 我刚才花了多少？ |
| [`status`](#status) | 这套东西到底接好了没有？ |
| [`providers`](#providers) | 本地都配了哪些厂商？ |
| `proxy` | 启动抓取代理(同 `llm-usage-proxy`)。 |

几条贯穿所有命令的约定：

- `--json` 输出和对应 MCP 工具完全一致的 Pydantic 结构，可以直接管道进 `jq`。
- `--color {auto,always,never}` 尊重 `NO_COLOR` 和 TTY 检测。配色是偏暖的低对比深色——半夜十一点看也不刺眼。
- 过滤参数(`--provider`、`--model`)对厂商名大小写不敏感、对模型名敏感，作为白名单时可以重复传。
- `--version` / `-V` 打印版本就退出。`--install-completion {bash|zsh|fish|powershell}` 装一份 Tab 补全脚本，重开一次 shell，所有参数都能 `<Tab>` 出来。

#### `compare`

给定一次 `n` 输入 / `m` 输出的调用，把每个有定价的模型按投影成本排个序，最便宜的在最上面，后面的标上相对倍数。默认会做「同族去重」：家族根相同**且**价格一致的行会合并——`gpt-5-mini` 和 `gpt-5-mini-2025-08-07` 折成一行，标个 `×2`。想看全部就加 `--all`。

```bash
# 今天跑一次 8k 输入 / 2k 输出，各家什么价？
$ llm-usage compare --in 8000 --out 2000

# 只看 OpenAI 这两个：
$ llm-usage compare --in 8000 --out 2000 --model gpt-5-mini --model gpt-5-nano

# 同样的投影，JSON 给脚本用：
$ llm-usage compare --in 8000 --out 2000 --json | jq '.ranked[0]'
```

#### `models`

定价表浏览器，`compare` 的姊妹命令——区别是它回答「这个模型怎么收费」，而不是「我的工作量会花多少」。单价按每百万 token 算，默认按厂商字母序排；用 `--sort input` 或 `--sort output` 切换，找某一边最便宜的。缓存单价默认藏起来(`--cache` 才显示)，因为大多数模型没有缓存价，空着的列纯属浪费屏幕宽度。

```bash
# 全表，已去重。
$ llm-usage models

# 只看 OpenAI 带 nano 的，连缓存价一起：
$ llm-usage models --provider openai --match nano --cache

# 输入单价从低到高——「现在地板价是多少」：
$ llm-usage models --sort input
```

#### `recommend`

替你挑一个。按 `--provider`、`--model`、`--budget` 过滤后，返回最便宜的那个，外加两个备选。它还会给一段说明，讲清楚自己假设了什么、为什么选这个——好让你能核对，而不是盲信。

```bash
# 最便宜的有价模型，没有别的条件。
$ llm-usage recommend

# 1k/1k 的调用，一分钱以内，Anthropic 家的：
$ llm-usage recommend --provider anthropic --budget 0.01

# 这三个里头，哪个胜出？
$ llm-usage recommend --model gpt-5-mini --model claude-sonnet-4-6 --model qwen-max
```

v1 只按成本排序。`--task` 是可选的，会出现在说明文字里，但**不影响**选择——这工具本身不是 LLM，读不懂自由文本。

#### `spend`

把 SQLite 读出来给你看。默认是一张 `usage_summary` 头条：总金额、Top-3 厂商、Top-3 模型、最贵的一次调用。加 `--group-by` 切到明细模式。

```bash
# 本周头条。
$ llm-usage spend

# 本月按模型分组，JSON 喂给看板：
$ llm-usage spend --period month --group-by model --json | jq

# 某个项目标签，逐天看：
$ llm-usage spend --group-by day --project my-side-thing
```

时段按自然 UTC 算：`today` = 今天 00:00 UTC 起，`week` = 本周一起，`month` = 本月 1 号起，`year` = 1 月 1 号起。失败的、流式中途断掉的行默认不计入；想看就加 `--include-failed`。

#### `status`

一屏，四块：数据库、抓取代理、厂商、定价。就是那个「到底都接好了没」的命令。**只读**——在全新安装、proxy 和 MCP 服务器都还没启动过的机器上跑它，会老老实实打印 `database not initialized`，而不是偷偷把文件建出来。

```bash
$ llm-usage status

# 跳过联网探测(离线、CI、网络慢)：
$ llm-usage status --no-net

# 机器可读，给健康检查脚本用：
$ llm-usage status --json
```

#### `providers`

按厂商看配置。比 `status` 里那块「厂商」更详细：多了协议格式标记(`openai-compat: yes/no`)，还能用 `--models` 展开，把每个厂商底下所有有价的模型都列出来。

```bash
$ llm-usage providers
$ llm-usage providers --models   # 展开每个厂商的模型清单
```

## MCP 工具一览

七个工具，通过 stdio 暴露。完整参数 / 返回值见 [`docs/spec.md`](docs/spec.md)。

| 工具 | 作用 |
|---|---|
| `query_spend` | 在一个时间窗口里统计总花费，可按 provider / model / project / tag / day 分组。 |
| `usage_summary` | 「今天 / 本周 / 本月 / 今年」一句话总结：总额、Top-3 厂商、Top-3 模型、最贵的一次。 |
| `compare_providers` | 给定一个假想工作量(输入 / 输出 token 数)，把所有有价模型按成本排序。 |
| `recommend_provider` | 在预算之内挑最便宜的模型。 |
| `get_pricing` | 直接查当前的定价快照。 |
| `list_providers` | 列出所有厂商、各自的模型、以及是否 OpenAI 兼容。 |
| `record_usage` | 手动写入一条记录——当 proxy 不在链路上时(离线分析、批量补录之类)。 |

`query_spend` 和 `usage_summary` 默认 `include_failed=false`，流式中断写下的部分计数行不会污染总额；想算进去就显式传 `true`。

## 架构

```
Layer 3:  src/llm_usage/mcp/       —— MCP 工具 + 资源(读路径)
Layer 2:  src/llm_usage/core/      —— SQLite + 定价 + 成本计算
Layer 1:  src/llm_usage/capture/   —— 抓取代理:Anthropic / OpenAI / DeepSeek / Qwen
```

中间是一个 SQLite(`~/.llm-usage/usage.db`)。Proxy 和 MCP 服务器是**两个独立进程**，之间没有 IPC，只是恰好指向同一个文件。成本在**写入那一刻**就按当时的定价快照算好，所以以后调价不会回头改写历史——这是事件溯源的标准做法，也是为什么我们敢把数字直接拿给你看。

流式抓取的做法是：**SSE 字节原样透传给客户端，旁路开一个 parser 把 `usage` 块攒出来**。Anthropic 走 `message_start` + `message_delta` 两段 usage，OpenAI 家族走最后一个带 `usage` 的 chunk(需要 `stream_options.include_usage=true`)。两条路径的细节都在 [`docs/architecture.md`](docs/architecture.md) 里。

## 支持的厂商(v1)

| 厂商 | 认证 | 非流式 | 流式 | 缓存计价 |
|---|---|---|---|---|
| Anthropic | `x-api-key` | 是 | 是 | `cache_creation` + `cache_read` |
| OpenAI | `Bearer` | 是 | 是 | 嵌套的 `prompt_tokens_details.cached_tokens` |
| DeepSeek | `Bearer` | 是 | 是 | `prompt_cache_hit_tokens` / `_miss_tokens` |
| Qwen (DashScope) | `Bearer` | 是 | 是 | OpenAI 兼容端点上通常不返回 |

定价数据是 [LiteLLM 定价 JSON](https://github.com/BerriAI/litellm/blob/main/litellm/model_prices_and_context_window_backup.json) 的精简快照，由一个 GitHub Action 每周自动刷新([`refresh-pricing.yml`](.github/workflows/refresh-pricing.yml))。

LiteLLM 还没收录的型号(比如 2026-05 之后才上的 `deepseek-v4-flash`)，我们用 [`pricing_overrides.json`](src/llm_usage/core/pricing_data/pricing_overrides.json) 在本地兜底，每次重启 proxy / MCP 服务器都会重新合并——改完那个文件重启就生效。

## 配置

所有配置都走环境变量(或仓库根目录的 `.env`)。默认值够用，启动 proxy 不强制要求任何一项。完整参考见 [`docs/configuration.md`](docs/configuration.md)。最常动的几个：

| 变量 | 默认值 | 作用 |
|---|---|---|
| `LLM_USAGE_DB_URL` | `sqlite:///$HOME/.llm-usage/usage.db` | 本地数据库路径。 |
| `LLM_USAGE_PROXY_PORT` | `5525` | 代理端口(永远 loopback)。 |
| `LLM_USAGE_ANTHROPIC_BASE_URL` | `https://api.anthropic.com` | 用第三方反代时改这里。 |
| `LLM_USAGE_OPENAI_BASE_URL` | `https://api.openai.com/v1` | 同上。 |
| `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / `DEEPSEEK_API_KEY` / `DASHSCOPE_API_KEY` | 未设置 | 各厂商 key，按需配。 |

## 开发

```bash
uv sync                                  # 装项目 + 开发依赖
uv run pytest                            # 700+ 个测试，约 6s
uv run ruff check src/ tests/            # lint
uv run ruff format --check src/ tests/   # 格式检查
uv run mypy                              # --strict
```

CI([`.github/workflows/ci.yml`](.github/workflows/ci.yml))在每个 PR 和每次推到 `main` 时都跑这四步，卡 80% 覆盖率底线。

## 想加新厂商？

参见 [`docs/post_v1_providers.md`](docs/post_v1_providers.md)(英文)，里面列了 Google Gemini、AWS Bedrock、Moonshot、Zhipu GLM、MiniMax、文心一言等等的接入工作量和坑。每加一家，有两笔账要分开算：

1. **定价数据**：在 `prices.json` / `pricing_overrides.json` 里加几行，小时级别。
2. **抓取适配器**：解析它特有的 usage 形状(尤其是流式)，天到周级别。

而且这两笔账**可以分开还**——先把定价加进来、用 `record_usage` 手工记着，等用量大了再写适配器，是个完全合理的中间状态。

## 许可证

[MIT](LICENSE)。
