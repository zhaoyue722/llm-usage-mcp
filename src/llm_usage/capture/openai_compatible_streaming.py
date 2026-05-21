"""Streaming (`stream: true`) capture for OpenAI / DeepSeek / Qwen.

The streaming sibling of `openai_compatible.py`. Same byte-fidelity
tee as the Anthropic streaming handler — raw upstream chunks flow to
the client untouched while a side-channel parser watches for the
usage data — but the OpenAI family's wire format makes this *simpler*
than Anthropic's, in two ways:

1. **Usage arrives once, at the end.** Anthropic splits usage across
   `message_start` + `message_delta`. The OpenAI family sends a
   single terminal chunk with `choices: []` and a populated `usage`
   field. So there's no multi-event accumulation — the accumulator
   just remembers the one chunk that carried `usage`, and the
   non-streaming token extractors (`_extract_openai` etc.) run on it
   unchanged.

2. **Failure rows never happen.** Because usage only lands at the
   end, a stream that dies mid-flight has *no* usage data — nothing
   honest to record, so we skip. (Anthropic can still record a
   `success=False` row on a mid-stream death because `message_start`
   delivered input tokens early; the OpenAI family can't.) The
   `ErrorType` values are therefore used only in skip-log messages
   here, never written to a row.

**The `include_usage` injection.** OpenAI-family streams omit `usage`
entirely unless the request carries `stream_options.include_usage:
true`. To capture anything, the proxy injects it into the request
body before forwarding — *unless* the client explicitly set
`include_usage` (true or false), in which case the client's choice
is respected. The cost: the client receives one extra SSE chunk it
didn't ask for (the `choices: []` usage chunk). That's a deliberate,
documented trade — harmless to compliant OpenAI clients, and the
byte-tee stays pure (we never drop or rewrite a chunk).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx
from fastapi import Request, Response
from fastapi.responses import StreamingResponse
from pydantic import SecretStr

from llm_usage.capture._openai_common import build_upstream_headers
from llm_usage.capture._streaming_common import (
    STREAMING_READ_TIMEOUT_S,
    UPSTREAM_CONNECT_TIMEOUT_S,
    ErrorType,
)
from llm_usage.core.db.session import get_session
from llm_usage.core.recording import record_event

if TYPE_CHECKING:
    # Imported for the type annotation only. A runtime import would be
    # circular: `openai_compatible.py` imports `handle_streaming` from
    # this module at module-load time.
    from llm_usage.capture.openai_compatible import ProviderRoute

logger = logging.getLogger(__name__)

# The literal sentinel the OpenAI family sends as its last SSE line.
# It is not JSON — the parser skips it rather than trying to decode.
_SSE_DONE_SENTINEL = "[DONE]"


# --- request-body injection -------------------------------------------------


def inject_include_usage(body: bytes) -> bytes:
    """Add `stream_options.include_usage: true` unless the client set it.

    Without this field the OpenAI family streams back no `usage` at
    all and the capture path would record nothing. An explicit client
    value — `true` *or* `false` — is left untouched: `false` means the
    caller deliberately opted out and we honor that (recording then
    just skips, since no usage chunk will arrive).

    A non-JSON or non-object body is returned unchanged — we don't
    second-guess it; the upstream will reject it with its own error.
    """
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        return body
    if not isinstance(parsed, dict):
        return body

    stream_options = parsed.get("stream_options")
    if isinstance(stream_options, dict):
        if "include_usage" in stream_options:
            return body  # client already decided — forward verbatim
        stream_options["include_usage"] = True
    else:
        parsed["stream_options"] = {"include_usage": True}
    return json.dumps(parsed).encode("utf-8")


# --- SSE parsing + accumulation ---------------------------------------------


class OpenAISSEParser:
    """Line-buffered parser for the OpenAI family's SSE wire format.

    The OpenAI family uses the *data-only* SSE subset: each event is a
    single `data: <json>` line followed by a blank line, and the
    stream ends with a literal `data: [DONE]`. No `event:` lines, no
    multi-line `data:`. `feed()` yields the raw JSON payload string of
    each `data:` line (the `[DONE]` sentinel is dropped). Buffers
    across chunk boundaries — a chunk can split a line mid-byte.
    """

    def __init__(self) -> None:
        self._buffer = bytearray()

    def feed(self, chunk: bytes) -> Iterator[str]:
        """Push bytes; yield the JSON payload of each completed `data:` line."""
        self._buffer.extend(chunk)
        while True:
            nl = self._buffer.find(b"\n")
            if nl < 0:
                break
            line_bytes = bytes(self._buffer[:nl])
            del self._buffer[: nl + 1]
            # SSE permits `\r\n`; strip the trailing `\r`.
            line = line_bytes.rstrip(b"\r").decode("utf-8", errors="replace")
            if not line.startswith("data:"):
                # Blank lines, comments (`:`), and any non-data field — skip.
                continue
            payload = line[len("data:") :].lstrip()
            if payload == _SSE_DONE_SENTINEL:
                continue
            yield payload


@dataclass
class OpenAIUsageAccumulator:
    """Captures the single SSE chunk that carries the `usage` block.

    OpenAI-family streaming delivers usage exactly once, in a terminal
    chunk shaped like a normal chat-completion object but with
    `choices: []`. The accumulator scans every `data:` payload and
    keeps the last one whose `usage` field is a non-empty dict. At
    stream end, `usage_chunk` is either that chunk (the call produced
    usage) or `None` (stream died early, or the client opted out of
    `include_usage`).
    """

    usage_chunk: dict[str, Any] | None = None

    def feed(self, data_str: str) -> None:
        """Consume one `data:` payload; keep it if it carries usage.

        Defensive against malformed payloads — a chunk that isn't JSON,
        or isn't an object, is silently ignored. The parser must never
        raise into the byte-stream path.
        """
        try:
            chunk = json.loads(data_str)
        except json.JSONDecodeError:
            return
        if not isinstance(chunk, dict):
            return
        usage = chunk.get("usage")
        # A non-empty `usage` dict marks the terminal usage chunk.
        # Pre-usage chunks carry `usage: null` or omit it entirely.
        if isinstance(usage, dict) and usage:
            self.usage_chunk = chunk


# --- HTTP handler -----------------------------------------------------------


async def handle_streaming(
    request: Request,
    route: ProviderRoute,
    body: bytes,
    key: SecretStr,
) -> Response:
    """Top-level orchestrator for one `stream: true` `/chat/completions` call.

    Caller (`openai_compatible.py:_handle_chat_completions`) has
    already resolved the API key (so `key` is non-None) and decided
    this is a streaming request. We inject `include_usage`, open the
    upstream as a streaming response, peek at the status, and split:
      - non-2xx → buffer the body, return a plain `Response` (the
        OpenAI family returns a one-shot JSON error envelope, not an
        SSE stream). No row written.
      - 2xx → return a `StreamingResponse` whose generator tees bytes
        to the client and records on the way out.
    """
    upstream_body = inject_include_usage(body)
    upstream_headers = build_upstream_headers(key)

    client: httpx.AsyncClient = request.app.state.http_client
    upstream_request = client.build_request(
        "POST",
        route.upstream_url,
        content=upstream_body,
        headers=upstream_headers,
        timeout=httpx.Timeout(STREAMING_READ_TIMEOUT_S, connect=UPSTREAM_CONNECT_TIMEOUT_S),
    )
    started_at = time.monotonic()
    upstream_resp = await client.send(upstream_request, stream=True)

    if upstream_resp.status_code >= 300:
        try:
            content = await upstream_resp.aread()
        finally:
            await upstream_resp.aclose()
        return Response(
            content=content,
            status_code=upstream_resp.status_code,
            media_type=upstream_resp.headers.get("content-type"),
        )

    return StreamingResponse(
        _iter_with_recording(upstream_resp, route, started_at),
        status_code=upstream_resp.status_code,
        media_type=upstream_resp.headers.get("content-type"),
    )


async def _iter_with_recording(
    upstream_resp: httpx.Response,
    route: ProviderRoute,
    started_at: float,
) -> AsyncIterator[bytes]:
    """Generator: yield upstream bytes to the client, capture usage on the side.

    Structurally parallel to the Anthropic streaming generator — the
    same five-way exception catch maps each failure to an `ErrorType`.
    The difference: `_record_or_skip` writes a row only when a usage
    chunk was actually observed, so every failure branch here ends in
    a skip (the OpenAI family delivers no usage until the terminal
    chunk). `error_type` therefore only colors the skip-log line.
    """
    accumulator = OpenAIUsageAccumulator()
    parser = OpenAISSEParser()
    try:
        async for chunk in upstream_resp.aiter_raw():
            for data_str in parser.feed(chunk):
                accumulator.feed(data_str)
            yield chunk
        _record_or_skip(accumulator, route, _duration_ms(started_at), error_type=None)
    except (asyncio.CancelledError, GeneratorExit):
        _record_or_skip(
            accumulator, route, _duration_ms(started_at), error_type="client_disconnect"
        )
        raise
    except httpx.ReadTimeout:
        _record_or_skip(accumulator, route, _duration_ms(started_at), error_type="timeout")
    except (httpx.RemoteProtocolError, httpx.ReadError):
        _record_or_skip(
            accumulator, route, _duration_ms(started_at), error_type="connection_dropped"
        )
    except Exception:
        logger.exception("unexpected error while proxying OpenAI-compatible stream")
        _record_or_skip(accumulator, route, _duration_ms(started_at), error_type="parse_error")
    finally:
        await upstream_resp.aclose()


# --- recording --------------------------------------------------------------


def _record_or_skip(
    accumulator: OpenAIUsageAccumulator,
    route: ProviderRoute,
    duration_ms: int,
    error_type: ErrorType | None,
) -> None:
    """Record a success row iff a usage chunk was seen; otherwise skip.

    A usage chunk means the call ran to completion and reported real
    counts — record a success row regardless of how the byte stream
    terminated afterward (a post-`[DONE]` connection drop is
    immaterial). No usage chunk means there is nothing honest to
    record:
      - `error_type` set   → the stream died before the terminal
        chunk; log a warning naming the failure.
      - `error_type` None  → the stream completed cleanly but carried
        no usage; the client opted out of `include_usage`. Log at
        INFO — this is expected, not a problem.

    Best-effort throughout: a recording exception is logged and
    swallowed so the user's streamed response is never affected.
    """
    if accumulator.usage_chunk is None:
        if error_type is not None:
            logger.warning(
                "%s stream ended (%s) before a usage chunk; skipping usage record",
                route.provider,
                error_type,
            )
        else:
            logger.info(
                "%s stream completed without a usage chunk "
                "(client opted out of stream_options.include_usage); skipping usage record",
                route.provider,
            )
        return

    kwargs = route.token_extractor(accumulator.usage_chunk)
    if kwargs is None:
        logger.warning(
            "%s usage chunk missing expected fields; skipping usage record",
            route.provider,
        )
        return
    kwargs["duration_ms"] = duration_ms

    try:
        with get_session() as session:
            record_event(session, **kwargs)
            session.commit()
    except Exception:
        logger.exception("failed to record %s streaming usage event", route.provider)


def _duration_ms(started_at: float) -> int:
    return int((time.monotonic() - started_at) * 1000)


__all__ = [
    "OpenAISSEParser",
    "OpenAIUsageAccumulator",
    "handle_streaming",
    "inject_include_usage",
]
