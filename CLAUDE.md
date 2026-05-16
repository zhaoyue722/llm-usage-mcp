# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

Local-first MCP server that captures LLM API usage across providers (OpenAI, Anthropic, Qwen, DeepSeek) and exposes spend queries as MCP tools. Full spec: @docs/spec.md

## Coding standards

- Python 3.13+, uv (not pip), ruff, mypy --strict
- No emojis anywhere (code, logs, prints)
- Test coverage ≥80% on core; pytest + respx for HTTP mocks
- Log token counts and metadata; full prompts only when `LLM_USAGE_LOG_PROMPTS=1`

## Workflow rules

- Before changing the spec or tool signatures, ask
- "I fixed it" is not enough — prove fixes with a failing-then-passing test
- When tempted to add a feature not in @docs/spec.md, do not — flag it instead
- Run `uv run pytest` before declaring any task complete

## Provider quirks (one-liners; full details: @docs/Provider_Adapter_Reference.md)

- **Anthropic**: streaming usage split across `message_start` + `message_delta`
- **OpenAI**: streaming needs `stream_options.include_usage=true`; `choices=[]` on usage chunk
- **Qwen**: OpenAI-compatible; pricing in CNY needs FX conversion
- **DeepSeek**: `prompt_cache_hit_tokens` / `prompt_cache_miss_tokens` (not OpenAI-style)

## Current focus

See @docs/detailed_plan.md
