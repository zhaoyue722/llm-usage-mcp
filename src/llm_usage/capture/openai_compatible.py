"""Non-streaming capture for OpenAI / DeepSeek / Qwen `/chat/completions`.

The three providers share the wire format almost completely:
- Same auth shape (`Authorization: Bearer …`).
- Same request body (`model`, `messages`, optional `stream`, etc.).
- Same non-streaming response envelope (`id`, `model`, `choices`,
  `usage`).
- Same SSE chunking shape for streaming (handled in a follow-up
  slice; this module 400-rejects `stream: true` for now).

They differ on **token accounting** inside the response's `usage`
block:
- **OpenAI** exposes `prompt_tokens` (total input including cached)
  and `prompt_tokens_details.cached_tokens` (the cached portion).
  The schema's `input_tokens` is the *uncached* portion, so we
  subtract.
- **DeepSeek** splits the cache count into two sibling fields:
  `prompt_cache_hit_tokens` (we map to `cache_read_tokens`) and
  `prompt_cache_miss_tokens` (we map to `input_tokens`). No
  `cache_creation` field — DeepSeek doesn't bill for cache writes
  separately, so `cache_write_tokens = 0` always.
- **Qwen** (DashScope OpenAI-compatible mode) usually omits
  `prompt_tokens_details` entirely; we treat missing as 0. The
  endpoint *can* return reasoning tokens for some models — those
  count inside `completion_tokens` and are billed at the output rate,
  so we don't need a separate field.

The variance lives in `TokenExtractor` callables — one per provider —
that take the response dict and return `record_event` kwargs. The
rest (header rewrite, JSON pre-flight for `stream`, upstream POST,
best-effort recording) is identical across all three, so it's
written once in `_handle_chat_completions`.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final, Literal

import httpx
from fastapi import APIRouter, Request, Response

from llm_usage.capture._openai_common import (
    STREAM_REJECTION_BODY,
    build_upstream_headers,
    missing_key_envelope,
    safe_parse_json,
)
from llm_usage.config import Settings
from llm_usage.core.db.session import get_session
from llm_usage.core.recording import record_event

logger = logging.getLogger(__name__)


# Narrow `Provider` to the subset this module serves. Anthropic gets
# its own dedicated module — passing "anthropic" here would be a
# routing bug, and the Literal makes mypy catch it at the call site
# in `proxy.py` rather than at runtime.
OpenAICompatProvider = Literal["openai", "deepseek", "qwen"]


# A token extractor turns a parsed response body into `record_event`
# kwargs. Return `None` when required fields are missing — the caller
# logs and skips, matching the Anthropic handler's best-effort posture.
TokenExtractor = Callable[[dict[str, Any]], dict[str, Any] | None]


@dataclass(frozen=True)
class _ProviderRoute:
    """Static per-provider data the request handler needs.

    Frozen so a typo in one of the closures captured at router-build
    time can't silently mutate later. Everything in here is derived
    from `Settings` + the provider name, but caching the resolved
    values means each request handler avoids re-deriving them.
    """

    provider: OpenAICompatProvider
    upstream_url: str
    env_var_name: str
    token_extractor: TokenExtractor


def build_router(settings: Settings, provider: OpenAICompatProvider) -> APIRouter:
    """Construct the `/v1/chat/completions` router for one provider.

    Called three times by `proxy.py` (once per OpenAI-compatible
    provider). The returned router is mounted with a provider prefix
    (`/openai`, `/deepseek`, `/qwen`) so the full proxy path is
    `/{provider}/v1/chat/completions` — symmetric with how the
    upstream SDKs assume `{base_url}/chat/completions`, and clear
    enough that a `curl` user knows which provider they're hitting
    from the URL alone.

    Per-provider differences are captured in a `_ProviderRoute`
    closure: which upstream URL to POST to, which env var holds the
    key (for the 503 message), and how to extract tokens from the
    response. The handler itself is identical across providers.
    """
    upstream_base = settings.base_url_for(provider).rstrip("/")
    route = _ProviderRoute(
        provider=provider,
        upstream_url=f"{upstream_base}/chat/completions",
        env_var_name=_env_var_for(provider),
        token_extractor=_TOKEN_EXTRACTORS[provider],
    )

    router = APIRouter()

    @router.post("/v1/chat/completions")
    async def chat_completions(request: Request) -> Response:
        return await _handle_chat_completions(request, settings, route)

    return router


async def _handle_chat_completions(
    request: Request,
    settings: Settings,
    route: _ProviderRoute,
) -> Response:
    """Top-level orchestrator for one `/chat/completions` call.

    Three short-circuits before any upstream contact:
      1. Missing API key for this provider → 503 with a configuration
         envelope (the proxy holds the key, so this is a server-side
         misconfig, not a client auth failure — 503, not 401).
      2. `stream: true` in the request body → 400 with the documented
         OpenAI-shaped envelope. Streaming is a follow-up slice.
      3. Anything else: forward to upstream, parse on the way back,
         write the event best-effort.
    """
    key = settings.api_key_for(route.provider)
    if key is None:
        return Response(
            content=json.dumps(missing_key_envelope(route.env_var_name)),
            status_code=503,
            media_type="application/json",
        )

    body = await request.body()

    parsed = safe_parse_json(body)
    if parsed is not None and parsed.get("stream") is True:
        return Response(
            content=json.dumps(STREAM_REJECTION_BODY),
            status_code=400,
            media_type="application/json",
        )

    upstream_headers = build_upstream_headers(key)

    started_at = time.monotonic()
    client: httpx.AsyncClient = request.app.state.http_client
    upstream_resp = await client.post(route.upstream_url, content=body, headers=upstream_headers)
    duration_ms = int((time.monotonic() - started_at) * 1000)

    if 200 <= upstream_resp.status_code < 300:
        _record_best_effort(upstream_resp, route, duration_ms)

    return Response(
        content=upstream_resp.content,
        status_code=upstream_resp.status_code,
        media_type=upstream_resp.headers.get("content-type"),
    )


def _record_best_effort(
    upstream_resp: httpx.Response,
    route: _ProviderRoute,
    duration_ms: int,
) -> None:
    """Parse the response and write an event. Swallow all errors.

    Recording is a side-effect; if it fails (DB locked, pricing
    missing, malformed response shape), the user's API call still
    succeeded and the response is on its way back to them. Log the
    exception and move on — never let the capture layer break the
    data path. Same posture as the Anthropic handler.
    """
    try:
        data = upstream_resp.json()
    except (json.JSONDecodeError, ValueError):
        logger.warning(
            "%s upstream response was not JSON; skipping usage record",
            route.provider,
        )
        return
    if not isinstance(data, dict):
        logger.warning(
            "%s upstream response was JSON but not an object; skipping usage record",
            route.provider,
        )
        return

    kwargs = route.token_extractor(data)
    if kwargs is None:
        logger.warning(
            "%s upstream response missing expected fields; skipping usage record",
            route.provider,
        )
        return
    kwargs["duration_ms"] = duration_ms

    try:
        with get_session() as session:
            record_event(session, **kwargs)
            session.commit()
    except Exception:
        logger.exception("failed to record %s usage event", route.provider)


# --- per-provider token extractors -----------------------------------------


_REQUIRED_FIELDS: Final[tuple[str, ...]] = ("id", "model", "usage")


def _basic_shape_ok(data: dict[str, Any]) -> bool:
    """Common shape check: `id`, `model`, `usage` all present and typed.

    Pulled out because all three extractors run it first and the
    failure mode (return None → caller logs + skips) is identical.
    """
    return (
        isinstance(data.get("id"), str)
        and isinstance(data.get("model"), str)
        and isinstance(data.get("usage"), dict)
    )


def _extract_openai(data: dict[str, Any]) -> dict[str, Any] | None:
    """OpenAI: subtract `cached_tokens` from `prompt_tokens` to get uncached.

    OpenAI bills the cached portion of the input at a discounted rate
    via `prompt_tokens_details.cached_tokens`, but reports
    `prompt_tokens` as the *total* (cached + uncached). Our schema's
    `input_tokens` is the uncached-input slot, so we subtract here.
    Missing `prompt_tokens_details` → treat cached as 0 (older models
    and many fine-tunes don't populate it).
    """
    if not _basic_shape_ok(data):
        return None
    usage = data["usage"]
    assert isinstance(usage, dict)
    cached = _extract_openai_cached_tokens(usage)
    prompt_tokens = int(usage.get("prompt_tokens") or 0)
    return {
        "provider": "openai",
        "model": data["model"],
        "input_tokens": max(0, prompt_tokens - cached),
        "output_tokens": int(usage.get("completion_tokens") or 0),
        "cache_write_tokens": 0,
        "cache_read_tokens": cached,
        "request_id": data["id"],
        "success": True,
    }


def _extract_openai_cached_tokens(usage: dict[str, Any]) -> int:
    """Defensive read of `usage.prompt_tokens_details.cached_tokens`.

    The field is nested two deep and either layer can be missing /
    null on older models or non-cache-eligible models. `or 0` collapses
    every "absent" shape (missing key, None, 0, empty dict) into 0.
    """
    details = usage.get("prompt_tokens_details")
    if not isinstance(details, dict):
        return 0
    return int(details.get("cached_tokens") or 0)


def _extract_deepseek(data: dict[str, Any]) -> dict[str, Any] | None:
    """DeepSeek: explicit `prompt_cache_hit_tokens` / `…_miss_tokens` split.

    DeepSeek's response is OpenAI-shaped on `id` / `model` / `choices`
    but its `usage` carries two siblings of `prompt_tokens` instead of
    the nested `prompt_tokens_details`:
        prompt_cache_hit_tokens  → our `cache_read_tokens`
        prompt_cache_miss_tokens → our `input_tokens`
    The two sum to `prompt_tokens`. No `cache_creation` field —
    DeepSeek bills cache writes at the regular input rate the first
    time tokens are written, so we never see a "creation" charge as a
    separate token count, and `cache_write_tokens = 0` is honest.
    """
    if not _basic_shape_ok(data):
        return None
    usage = data["usage"]
    assert isinstance(usage, dict)
    return {
        "provider": "deepseek",
        "model": data["model"],
        "input_tokens": int(usage.get("prompt_cache_miss_tokens") or 0),
        "output_tokens": int(usage.get("completion_tokens") or 0),
        "cache_write_tokens": 0,
        "cache_read_tokens": int(usage.get("prompt_cache_hit_tokens") or 0),
        "request_id": data["id"],
        "success": True,
    }


def _extract_qwen(data: dict[str, Any]) -> dict[str, Any] | None:
    """Qwen (DashScope compatible mode): OpenAI-shaped, cache fields often absent.

    DashScope's OpenAI-compatible endpoint returns `prompt_tokens` and
    `completion_tokens` but typically omits `prompt_tokens_details`
    entirely. We treat that as zero cache hits and zero cache writes
    — accurate for v1 (cache pricing in our internal model already
    defaults to 0 / None for Qwen rows). The native DashScope API has
    richer fields (`output_tokens_details.thoughts_count` for
    reasoning models) but those aren't in scope here.
    """
    if not _basic_shape_ok(data):
        return None
    usage = data["usage"]
    assert isinstance(usage, dict)
    cached = _extract_openai_cached_tokens(usage)  # same nested-shape read
    prompt_tokens = int(usage.get("prompt_tokens") or 0)
    return {
        "provider": "qwen",
        "model": data["model"],
        "input_tokens": max(0, prompt_tokens - cached),
        "output_tokens": int(usage.get("completion_tokens") or 0),
        "cache_write_tokens": 0,
        "cache_read_tokens": cached,
        "request_id": data["id"],
        "success": True,
    }


_TOKEN_EXTRACTORS: Final[dict[OpenAICompatProvider, TokenExtractor]] = {
    "openai": _extract_openai,
    "deepseek": _extract_deepseek,
    "qwen": _extract_qwen,
}


def _env_var_for(provider: OpenAICompatProvider) -> str:
    """Map provider name → the env var the user sets the key in.

    Used in the 503 response message so a misconfigured user sees
    exactly which variable to set. Anthropic has its own module; this
    map covers the three OpenAI-compatible providers.
    """
    return {
        "openai": "OPENAI_API_KEY",
        "deepseek": "DEEPSEEK_API_KEY",
        "qwen": "DASHSCOPE_API_KEY",
    }[provider]


__all__ = ["build_router"]
