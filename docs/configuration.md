# Configuration

Every knob `llm-usage-mcp` exposes lives in one place: the `Settings`
class in [`src/llm_usage/config.py`](../src/llm_usage/config.py).
Values come from environment variables (with an optional `.env` file
in the current working directory as a fallback).

Settings are read **once per process** via `get_settings()` — the
result is `lru_cache`'d. If you change an env var at runtime (in
tests, for example), call `get_settings.cache_clear()` to force a
re-read.

## Quickstart

1. Copy [`.env.example`](../.env.example) to `.env` at the repo root.
2. Fill in the keys you need for the providers you plan to use.
3. (Optional) Override the DB path with `LLM_USAGE_DB_URL`.

The example file is checked in; `.env` is gitignored.

## Env-var reference

| Variable | Type | Default | Purpose |
|---|---|---|---|
| `LLM_USAGE_DB_URL` | SQLAlchemy URL | `sqlite:///$HOME/.llm-usage/usage.db` | Location of the local usage database. |
| `LLM_USAGE_LOG_LEVEL` | enum | `INFO` | One of `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`. |
| `LLM_USAGE_LOG_PROMPTS` | bool | `false` | When true, the capture layer stores full prompts in event metadata. Off by default for privacy. |
| `LLM_USAGE_PROXY_PORT` | int (1–65535) | `5525` | TCP port for the capture proxy. Always bound to `127.0.0.1` (loopback) so the proxy is never reachable from the network. |
| `LLM_USAGE_ENABLED_PROVIDERS` | CSV | `anthropic,openai,qwen,deepseek` | Providers the proxy / capture layer is expected to serve. Drives `Settings.require_keys()`. |
| `LLM_USAGE_ANTHROPIC_BASE_URL` | URL | `https://api.anthropic.com` | Override the Anthropic endpoint (gateways, test doubles, regional hosts). |
| `LLM_USAGE_OPENAI_BASE_URL` | URL | `https://api.openai.com/v1` | OpenAI endpoint override. |
| `LLM_USAGE_QWEN_BASE_URL` | URL | `https://dashscope.aliyuncs.com/compatible-mode/v1` | Qwen / DashScope OpenAI-compatible endpoint override. |
| `LLM_USAGE_DEEPSEEK_BASE_URL` | URL | `https://api.deepseek.com` | DeepSeek endpoint override. |
| `ANTHROPIC_API_KEY` | secret | unset | Anthropic API key. Stored as `SecretStr` — not printed in reprs/logs. |
| `OPENAI_API_KEY` | secret | unset | OpenAI API key. |
| `DASHSCOPE_API_KEY` | secret | unset | Qwen API key (read from the DashScope-standard env var name). |
| `DEEPSEEK_API_KEY` | secret | unset | DeepSeek API key. |

## Per-provider keys: where they're read from

We deliberately use the **SDK-standard names** for API keys instead of
namespacing them under `LLM_USAGE_*`. This way your existing
`ANTHROPIC_API_KEY` already works without extra plumbing, and there's
no ambiguity about which value the official Anthropic SDK and our
capture layer would pick up.

| Provider (our name) | Env var read | Standard upstream tool |
|---|---|---|
| `anthropic` | `ANTHROPIC_API_KEY` | Anthropic Python / TS SDK |
| `openai` | `OPENAI_API_KEY` | OpenAI Python / TS SDK |
| `qwen` | `DASHSCOPE_API_KEY` | DashScope SDK; Qwen docs |
| `deepseek` | `DEEPSEEK_API_KEY` | DeepSeek docs |

Programmatic access:

```python
from llm_usage.config import get_settings

settings = get_settings()
key = settings.api_key_for("anthropic")   # SecretStr | None
base = settings.base_url_for("openai")    # str
```

## Refuse-to-start: `Settings.require_keys()`

`require_keys()` raises `ConfigurationError` if **any provider listed
in `LLM_USAGE_ENABLED_PROVIDERS` is missing its API key**. The error
names every missing provider and the env var that needs to be set.

```python
from llm_usage.config import ConfigurationError, get_settings

try:
    get_settings().require_keys()
except ConfigurationError as exc:
    sys.exit(f"config error: {exc}")
```

This is not invoked automatically anywhere — it is an opt-in gate.
The MCP server, CLI, and direct library use stay usable without
provider keys (you can query historical spend without any keys in
hand). The capture proxy and SDK wrappers, which need keys to make
outbound calls, should call `require_keys()` at startup.

To intentionally skip a provider, narrow `LLM_USAGE_ENABLED_PROVIDERS`:

```bash
LLM_USAGE_ENABLED_PROVIDERS=openai,anthropic
```

Then `require_keys()` only enforces those two.

## `.env` file loading

`Settings` looks for a `.env` file in the **current working directory**
of the process. Process environment beats `.env` (pydantic-settings
default precedence). The file uses `KEY=VALUE` syntax, one per line;
quote values with embedded whitespace.

```dotenv
LLM_USAGE_DB_URL=sqlite:///./usage.db
LLM_USAGE_LOG_LEVEL=DEBUG
ANTHROPIC_API_KEY=sk-ant-...
```

`.env` is gitignored. **Never commit a real `.env`**; share the
template via `.env.example` instead.

## Security note

API-key fields are `SecretStr`; `repr()` and `str()` redact them, and
they don't appear in pydantic validation-error messages. To read the
plaintext, call `.get_secret_value()` — do this only at the request
boundary (e.g., when building an Authorization header).
