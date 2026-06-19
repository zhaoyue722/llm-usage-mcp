# llm-usage-mcp

> [English](README.md) | 中文

你在各家大模型上到底花了多少钱？这个工具帮你算清楚，全程在本地，不上云。查账的时候，既能让编码 Agent 替你开口问（[MCP](https://modelcontextprotocol.io)），也能自己敲命令行（CLI）。

[![CI](https://github.com/zhaoyue722/llm-usage-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/zhaoyue722/llm-usage-mcp/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.13+](https://img.shields.io/badge/python-3.13%2B-blue.svg)](https://www.python.org/downloads/)

## 为什么你需要这个工具

现在做开发，手里多半同时开着好几家大模型：Claude、GPT，还有通义千问、DeepSeek。钱花得悄无声息，可你想回头算笔账却没那么容易：每家一个控制台，计价货币还不统一（有的算美元、有的算人民币），就连「一个缓存 token 该收多少钱」，四家都有四套各自的算法。

于是「这个月到底花了多少、花在哪了」这么个再正常不过的问题，真查起来得登四个后台、自己换一遍汇率、再把四套缓存规则对齐。多数人嫌麻烦，干脆就不查了，然后等月底账单来给你个「惊喜」。

`llm-usage-mcp` 做的事很直接：你每调一次，它就记一次，而且在调用发生的当下就按对应厂商的价目把这笔钱算准。要看账的时候，两种姿势随你挑：

- **张嘴问 Agent。** 它本身是个 MCP 服务器，Claude Code、Cursor 这些 MCP 客户端都听得懂大白话：「这周在 Claude 上花了多少？」「跑一次 10k 输入 / 2k 输出，哪家最划算？」
- **或者敲命令。** 它也是个命令行工具，`llm-usage spend`、`llm-usage compare`、`llm-usage recommend` 几条命令直接出结果，不想绕着 Agent 问的时候就用它。

用着也省心：

- **数据只在你自己机器上。** 不连 SaaS、不用注册、不上报任何东西，所有记录就躺在 `~/.llm-usage/usage.db` 这一个 SQLite 文件里。隐私是天生的，不是某个要你专门去打开的开关。
- **多家厂商，国产模型一样待见。** Anthropic、OpenAI、DeepSeek、Qwen 四家都支持，流式非流式都不落下；DeepSeek 和 Qwen 跟海外厂商同等对待，不是补丁式的事后支持。

## 两分钟跑起来

从 `git clone` 到记下第一笔调用，大概两分钟。这一节讲的是怎么把调用**记下来**；记下来之后[怎么查账](#查看你的花费)，下一节再说。

### 1. 安装

```bash
git clone https://github.com/zhaoyue722/llm-usage-mcp.git
cd llm-usage-mcp
uv sync
```

`uv sync` 会把项目和开发依赖一起装好，顺手在 venv 里建三个命令：

- `llm-usage`：主命令行工具，七个子命令，详见后面的 [查看你的花费](#查看你的花费) 一节。
- `llm-usage-mcp`：stdio 模式的 MCP 服务器。
- `llm-usage-proxy`：抓取代理。其实就是 `llm-usage proxy` 的别名，为了向后兼容留着。

> 还没装 [uv](https://docs.astral.sh/uv/) 的话，`pip install uv` 装一次就有，国内建议配个镜像源。Python 版本需要 3.13 以上。

### 2. 至少配一个厂商的 Key

只给**你真正会用到**的厂商配 key 就行。proxy 不会因为你某家没配就不启动：没配 key 的那条路由会单独报 `503 configuration_error`，其余照常用。

```bash
# 海外(需要能访问境外网络)
export ANTHROPIC_API_KEY=sk-ant-...
export OPENAI_API_KEY=sk-...

# 国内(直连就行)
export DEEPSEEK_API_KEY=sk-...
export DASHSCOPE_API_KEY=sk-...   # 通义千问，在阿里云 DashScope 控制台拿
```

> **关于网络。** Anthropic 和 OpenAI 的官方接口在国内一般直连不了，得走境外网络。如果你用的是第三方反代或自建网关，把对应的 `*_BASE_URL` 指过去就行（见 [`docs/configuration.md`](docs/configuration.md)）。DeepSeek 和 DashScope 走国内公网，直连没问题。

完整的环境变量清单见 [`docs/configuration.md`](docs/configuration.md)；嫌翻文档麻烦，也可以把 [`.env.example`](.env.example) 复制成 `.env` 填一填。

### 3. 启动抓取代理

```bash
uv run llm-usage-proxy
```

代理**只绑本机回环地址**（`127.0.0.1:5525`），同一个局域网里的其他机器都连不上它。这点是写死在代码里的，不是配置项：既然主打「本地优先、隐私默认」，就不能留个一手滑就把代理暴露到公网的口子。API Key 全由 proxy 这边保管，客户端那头一个 key 都不用填。

### 4. 把 Agent 的请求指向代理

每家厂商一条路由。在客户端那一侧，把对应的 `*_BASE_URL` 指过来：

| 厂商 | 客户端环境变量 | 值 |
|---|---|---|
| Anthropic | `ANTHROPIC_BASE_URL` | `http://127.0.0.1:5525` |
| OpenAI | `OPENAI_BASE_URL` | `http://127.0.0.1:5525/openai/v1` |
| DeepSeek | `DEEPSEEK_BASE_URL`（或任何 OpenAI SDK 的 base-url 覆盖） | `http://127.0.0.1:5525/deepseek/v1` |
| Qwen | DashScope 的 OpenAI 兼容 base | `http://127.0.0.1:5525/qwen/v1` |

举个例子，让 Claude Code 的请求全走代理：

```bash
ANTHROPIC_BASE_URL=http://127.0.0.1:5525 claude
```

再比如，用 OpenAI SDK 调通义千问，一样会被记下来：

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

### 5. 确认它在记账

通过 Agent（或者任何指向代理的客户端）随便发一次调用，然后看看有没有记上：

```bash
uv run llm-usage spend
```

每次调用都会连着 token 数、花了多少钱（精确到纳美元）、耗时、还有一个去重用的 `request_id`，一起写进 `~/.llm-usage/usage.db`，也会出现在这张周报里。一条闭环就齐了：一头记账，一头看账。

## 查看你的花费

调用开始被记下来之后，有两种方式把账读回来。数据是同一份、数字也一样，看你当下顺手用哪个。

### 让编码 Agent 帮你查（MCP）

把 MCP 服务器挂到 Claude Code 上：

```bash
claude mcp add llm-usage -- uv --directory $(pwd) run llm-usage-mcp
```

然后在那个会话里张嘴就问：

> 今天我在 Anthropic 上花了多少？跑一次 10k 输入 / 2k 输出，哪家最便宜？

剩下的交给 Claude：它会自己挑合适的工具、把数字读回来。一共七个工具，通过 stdio 暴露；完整的参数和返回值见 [`docs/spec.md`](docs/spec.md)。

| 工具 | 作用 |
|---|---|
| `query_spend` | 给定时间窗口统计总花费，可按 provider / model / project / tag / day 分组。 |
| `usage_summary` | 「今天 / 本周 / 本月 / 今年」的一句话总结：总额、花得最多的 3 家厂商和 3 个模型、最贵的一次。 |
| `compare_providers` | 给定一个假想工作量（输入 / 输出 token 数），把所有有价模型按成本排序。 |
| `recommend_provider` | 在预算之内挑最便宜的模型。 |
| `get_pricing` | 直接查当前的定价快照。 |
| `list_providers` | 列出所有厂商、各自的模型、以及是否 OpenAI 兼容。 |
| `record_usage` | 手动记一条调用——proxy 不在链路上时用（比如离线分析、批量补录）。 |

`query_spend` 和 `usage_summary` 默认 `include_failed=false`，流式中断时写下的半截记录不会混进总额；想把它们也算进来，显式传 `true` 即可。

### 用命令行查（CLI）

同样这些问题，换成命令行——七个子命令，全塞在一个 `llm-usage` 命令底下。有时候自己敲一行，比开口问 Agent 还快。

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
| [`models`](#models) | 每百万 token 各家到底收多少？ |
| [`recommend`](#recommend) | 别让我选了，直接给我挑一个。 |
| [`spend`](#spend) | 我刚才花了多少？ |
| [`status`](#status) | 这套东西到底接上了没有？ |
| [`providers`](#providers) | 本地都配了哪些厂商？ |
| `proxy` | 启动抓取代理（同 `llm-usage-proxy`）。 |

几条所有命令通用的约定：

- `--json`：输出和对应 MCP 工具一模一样的 Pydantic 结构，可以直接管道给 `jq`。
- `--color {auto,always,never}`：认 `NO_COLOR`，也会自动判断当前是不是 TTY。配色用的是偏暖的低对比深色，半夜十一点盯着也不晃眼。
- 过滤参数（`--provider`、`--model`）：厂商名不分大小写，模型名分；作为白名单用时可以重复传。
- `--version` / `-V`：打印版本就退出。`--install-completion {bash|zsh|fish|powershell}`：装一份 Tab 补全脚本，重开一次 shell，所有参数都能 `<Tab>` 补出来。

#### `compare`

给定一次 `n` 输入 / `m` 输出的调用，把每个有定价的模型按预估成本排个序：最便宜的排最上面，后面的标上相对倍数。默认会做「同族合并」：家族根相同、价格又一样的行会折成一条，比如 `gpt-5-mini` 和 `gpt-5-mini-2025-08-07` 并成一行，标个 `×2`。想看全部就加 `--all`。

```bash
# 今天跑一次 8k 输入 / 2k 输出，各家什么价？
$ llm-usage compare --in 8000 --out 2000

# 只看 OpenAI 这两个：
$ llm-usage compare --in 8000 --out 2000 --model gpt-5-mini --model gpt-5-nano

# 同样的预估，要 JSON 喂给脚本：
$ llm-usage compare --in 8000 --out 2000 --json | jq '.ranked[0]'
```

#### `models`

定价表浏览器，跟 `compare` 是一对：它回答的是「这个模型怎么收费」，而不是「我这摊活儿要花多少」。单价按每百万 token 列，默认按厂商名字母排；想找某一头最便宜的，用 `--sort input` 或 `--sort output`。缓存单价默认不显示（加 `--cache` 才出来），因为大多数模型压根没有缓存价，空着的列只会白占宽度。

```bash
# 全表，已去重。
$ llm-usage models

# 只看 OpenAI 带 nano 的，顺带缓存价：
$ llm-usage models --provider openai --match nano --cache

# 输入单价从低到高，看看「现在的地板价」：
$ llm-usage models --sort input
```

#### `recommend`

替你拿主意。先按 `--provider`、`--model`、`--budget` 筛一遍，再给出最便宜的那个，外加两个备选。它还会附一段说明，讲清楚它假设了什么、为什么挑这个，方便你核对，而不是让你闭着眼睛信。

```bash
# 最便宜的有价模型，没别的条件。
$ llm-usage recommend

# 1k/1k 的调用，一分钱以内，只要 Anthropic 家的：
$ llm-usage recommend --provider anthropic --budget 0.01

# 这三个里头，谁赢？
$ llm-usage recommend --model gpt-5-mini --model claude-sonnet-4-6 --model qwen-max
```

v1 只按价格排序。`--task` 是可选的，会写进说明文字，但不参与选型：这工具又不是 LLM，读不懂你那段自由描述。

#### `spend`

把 SQLite 里的账读给你看。默认是一张 `usage_summary` 头条：总金额、花得最多的 3 家厂商、3 个模型、还有最贵的那一次调用。想看明细，加 `--group-by`。

```bash
# 本周头条。
$ llm-usage spend

# 本月按模型分组，JSON 喂给看板：
$ llm-usage spend --period month --group-by model --json | jq

# 看某个项目标签，一天一行：
$ llm-usage spend --group-by day --project my-side-thing
```

时段按自然 UTC 算：`today` 是今天 00:00 UTC 起，`week` 从本周一起，`month` 从 1 号起，`year` 从 1 月 1 号起。失败的、流式中途断掉的记录默认不算进去；想算就加 `--include-failed`。

#### `status`

一屏四块：数据库、抓取代理、厂商、定价。就是那条「到底都接上了没」的命令。它**只读**：在一台全新的、proxy 和 MCP 服务器都还没跑过的机器上执行它，它会老老实实告诉你 `database not initialized`，而不会偷偷把数据库文件给你建出来。

```bash
$ llm-usage status

# 跳过联网探测（离线、CI、或者网慢的时候）：
$ llm-usage status --no-net

# 要机器可读的，给健康检查脚本用：
$ llm-usage status --json
```

#### `providers`

按厂商看配置，比 `status` 里那一块更细：多了协议格式标记（`openai-compat: yes/no`），还能用 `--models` 展开，把每家底下所有有价的模型都列出来。

```bash
$ llm-usage providers
$ llm-usage providers --models   # 把每家厂商连同它的模型清单一起展开
```

## 支持的厂商

| 厂商 | 认证 | 非流式 | 流式 | 缓存计价 |
|---|---|---|---|---|
| Anthropic | `x-api-key` | 是 | 是 | `cache_creation` + `cache_read` |
| OpenAI | `Bearer` | 是 | 是 | 嵌套的 `prompt_tokens_details.cached_tokens` |
| DeepSeek | `Bearer` | 是 | 是 | `prompt_cache_hit_tokens` / `_miss_tokens` |
| Qwen (DashScope) | `Bearer` | 是 | 是 | OpenAI 兼容端点上通常不返回 |

**还有更多在路上。** Google Gemini、AWS Bedrock、Moonshot（Kimi）、Zhipu GLM、MiniMax、文心一言这些都排在计划里，具体工作量和坑见 [`docs/post_v1_providers.md`](docs/post_v1_providers.md)（英文）。

**价格是哪来的。** 定价取自 [LiteLLM 的定价 JSON](https://github.com/BerriAI/litellm/blob/main/litellm/model_prices_and_context_window_backup.json) 的精简快照，有个 GitHub Action 每周自动拉一次最新的（[`refresh-pricing.yml`](.github/workflows/refresh-pricing.yml)）。LiteLLM 还没收录的型号（比如 2026-05 之后才上线的 `deepseek-v4-flash`），用 [`pricing_overrides.json`](src/llm_usage/core/pricing_data/pricing_overrides.json) 在本地补上。

## 配置

配置全走环境变量（或者仓库根目录下的 `.env`）。默认值就够用，启动 proxy 不强制要求任何一项。完整清单见 [`docs/configuration.md`](docs/configuration.md)。最可能动的三个：

| 变量 | 默认值 | 作用 |
|---|---|---|
| `LLM_USAGE_DB_URL` | `sqlite:///$HOME/.llm-usage/usage.db` | 本地数据库存哪。 |
| `LLM_USAGE_PROXY_PORT` | `5525` | 代理端口（永远只绑回环）。 |
| `LLM_USAGE_<PROVIDER>_BASE_URL` | 各家官方端点 | 把某家指到反代 / 网关，国内网络受限时很有用。 |

## 许可证

[MIT](LICENSE)。
