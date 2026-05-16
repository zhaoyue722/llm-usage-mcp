"""Anthropic `/v1/messages` route for the capture proxy.

The proxy is a thin reverse-proxy: receive a request from the caller,
forward it to `{settings.anthropic_base_url}/v1/messages`, return the
upstream response verbatim, and — on success — record the token usage
to the local `usage_events` table. Capture is **best-effort**: a
recording failure (DB locked, pricing missing, anything) must not
turn into a user-visible error on the LLM call. The proxy's job is to
be transparent.

Header policy is a **whitelist**, not a blacklist. The upstream request
is built from scratch with `x-api-key` set to the server-side
configured value (never the client's, per the Phase 1 auth decision),
plus `anthropic-version` (forwarded or defaulted) and `anthropic-beta`
if present. Everything else from the client — `Authorization`,
cookies, hop-by-hop headers, custom telemetry headers — is dropped.

Phase 1 explicitly **rejects** `stream: true` requests with a 400.
Forwarding without parsing would be silent under-counting (the proxy
would return SSE chunks to the client but never see the final
`message_delta` usage block). Streaming support is Phase 2's job; the
400 tells callers to set `stream: false` or hit Anthropic directly
until then.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Mapping
from typing import Any, Final

import httpx
from fastapi import APIRouter, Request, Response

from llm_usage.config import Settings
from llm_usage.core.db.session import get_session
from llm_usage.core.recording import record_event

logger = logging.getLogger(__name__)

# Default value injected when the client doesn't pass an
# `anthropic-version` header. Matches Anthropic's documented stable
# version; the proxy forwards whatever the client sent if they did.
_ANTHROPIC_VERSION_DEFAULT: Final[str] = "2023-06-01"

# Body returned for `stream: true` requests in Phase 1. Shaped like
# Anthropic's own error envelope so a well-behaved client can parse it
# the same way it would parse a real `invalid_request_error`.
_STREAM_REJECTION_BODY: Final[dict[str, Any]] = {
    "type": "error",
    "error": {
        "type": "invalid_request_error",
        "message": (
            "Streaming is not yet supported by the local capture proxy. "
            "Set stream: false to record usage through this proxy, or call "
            "api.anthropic.com directly while streaming support lands in a "
            "follow-up release."
        ),
    },
}


def build_router(settings: Settings) -> APIRouter:
    """Construct the `/v1/messages` router bound to a `Settings` instance.

    Taking `settings` as an explicit argument (rather than calling
    `get_settings()` inside the handler) keeps the router unit-testable
    against a fake upstream URL / fake API key without monkey-patching
    the module-level cache.
    """
    router = APIRouter()

    @router.post("/v1/messages")
    async def messages(request: Request) -> Response:
        return await _handle_messages(request, settings)

    return router


async def _handle_messages(request: Request, settings: Settings) -> Response:
    """Top-level orchestrator for one `/v1/messages` call."""
    body = await request.body()

    parsed = _safe_parse_json(body)
    if parsed is not None and parsed.get("stream") is True:
        return Response(
            content=json.dumps(_STREAM_REJECTION_BODY),
            status_code=400,
            media_type="application/json",
        )

    upstream_url = f"{settings.anthropic_base_url.rstrip('/')}/v1/messages"
    upstream_headers = _build_upstream_headers(request.headers, settings)

    started_at = time.monotonic()
    client: httpx.AsyncClient = request.app.state.http_client
    upstream_resp = await client.post(upstream_url, content=body, headers=upstream_headers)
    duration_ms = int((time.monotonic() - started_at) * 1000)

    if 200 <= upstream_resp.status_code < 300:
        _record_best_effort(upstream_resp, duration_ms)

    return Response(
        content=upstream_resp.content,
        status_code=upstream_resp.status_code,
        media_type=upstream_resp.headers.get("content-type"),
    )


def _build_upstream_headers(
    client_headers: Mapping[str, str],
    settings: Settings,
) -> dict[str, str]:
    """Whitelist client headers; always overwrite `x-api-key` server-side.

    Anthropic authenticates via `x-api-key`, not `Authorization: Bearer`.
    Phase 1's auth decision: the proxy never forwards the client's auth.
    The user's coding agent doesn't need to know the API key — the proxy
    holds it. `assert key is not None` is justified because the proxy
    startup path (`run_proxy()`) calls `Settings.require_keys({"anthropic"})`
    before binding the port, so any path that reaches this function with
    a missing key is a bug, not a runtime error.
    """
    key = settings.api_key_for("anthropic")
    assert key is not None, "require_keys() should have caught the missing ANTHROPIC_API_KEY"

    headers: dict[str, str] = {
        "x-api-key": key.get_secret_value(),
        "anthropic-version": client_headers.get("anthropic-version", _ANTHROPIC_VERSION_DEFAULT),
        "content-type": "application/json",
    }
    if (beta := client_headers.get("anthropic-beta")) is not None:
        headers["anthropic-beta"] = beta
    return headers


def _safe_parse_json(body: bytes) -> dict[str, Any] | None:
    """Best-effort JSON parse for the `stream` check.

    A non-JSON or non-object body is fine — we let Anthropic upstream
    return its own validation error instead of trying to second-guess.
    Only used for the `stream: true` pre-flight; the actual forwarding
    uses the raw `body` bytes so we don't re-serialize.
    """
    if not body:
        return None
    try:
        result = json.loads(body)
    except json.JSONDecodeError:
        return None
    if not isinstance(result, dict):
        return None
    return result


def _record_best_effort(upstream_resp: httpx.Response, duration_ms: int) -> None:
    """Parse the upstream response and write an event. Swallow all errors.

    Recording is a side-effect; if it fails (DB locked, pricing
    missing, malformed response shape), the user's API call still
    succeeded and the response is on its way back to them. Log the
    exception and move on — never let the capture layer break the data
    path.
    """
    try:
        data = upstream_resp.json()
    except (json.JSONDecodeError, ValueError):
        logger.warning("upstream response was not JSON; skipping usage record")
        return
    if not isinstance(data, dict):
        logger.warning("upstream response was JSON but not an object; skipping usage record")
        return

    kwargs = _response_to_event_args(data, duration_ms)
    if kwargs is None:
        logger.warning("upstream response missing expected fields; skipping usage record")
        return

    try:
        with get_session() as session:
            record_event(session, **kwargs)
            session.commit()
    except Exception:
        logger.exception("failed to record Anthropic usage event")


def _response_to_event_args(data: dict[str, Any], duration_ms: int) -> dict[str, Any] | None:
    """Map Anthropic's `/v1/messages` response to `record_event` kwargs.

    Returns `None` when required fields (`id`, `model`, `usage`) are
    missing or wrong-shaped — `_record_best_effort` logs and skips.
    Cache token mapping per Anthropic's spec:
    `cache_creation_input_tokens` → our `cache_write_tokens`,
    `cache_read_input_tokens` → our `cache_read_tokens`. Both default
    to 0 when absent (a model without prompt caching just omits them).
    The `id` (e.g. `msg_01ABCDE...`) becomes `request_id` so replays
    of the same response don't double-count via the `usage_events`
    UNIQUE index.
    """
    model = data.get("model")
    usage = data.get("usage")
    msg_id = data.get("id")
    if not isinstance(model, str) or not isinstance(usage, dict) or not isinstance(msg_id, str):
        return None

    return {
        "provider": "anthropic",
        "model": model,
        "input_tokens": int(usage.get("input_tokens") or 0),
        "output_tokens": int(usage.get("output_tokens") or 0),
        "cache_write_tokens": int(usage.get("cache_creation_input_tokens") or 0),
        "cache_read_tokens": int(usage.get("cache_read_input_tokens") or 0),
        "request_id": msg_id,
        "duration_ms": duration_ms,
        "success": True,
    }


__all__ = ["build_router"]
