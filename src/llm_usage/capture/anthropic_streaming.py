"""Streaming (`stream: true`) variant of the Anthropic `/v1/messages` proxy.

The non-streaming sibling (`anthropic.py`) handles the simple case:
buffer the full response, parse JSON, write one event. Streaming is
harder because the bytes flow chunk-by-chunk to the client *while* we
extract usage data on a side channel. The two requirements are in
tension and the design has to honor both:

  1. **Byte-fidelity to the client.** The bytes that reach the user's
     coding agent must be exactly what Anthropic sent — same chunking
     is not required, but ordering, line endings, and event shape are.
     We never re-serialize. We tee: one path yields raw `aiter_raw()`
     bytes to FastAPI's `StreamingResponse`; the other path feeds a
     copy of those bytes to a tiny line-buffered SSE parser.
  2. **One source of truth for partial counts.** Whatever leg fails
     (upstream `event: error`, upstream TCP drop, client disconnect,
     read timeout, parser bug) reads from the same accumulator state.
     No parallel parsers, no rebuilt running totals — the row that
     gets written reflects the last `message_delta` we observed,
     period.

`error_type` is a fixed enum (see `ErrorType` below). A failure row
always has `request_id=NULL` so a successful retry of the same logical
call (with a fresh `msg_…` id from Anthropic) cannot UNIQUE-conflict
with our recorded failure. Output tokens on a failed stream reflect
the last `message_delta` observed and may underreport actual billing
(noted in `docs/architecture.md`).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field

import httpx
from fastapi import Request, Response
from fastapi.responses import StreamingResponse

from llm_usage.capture._anthropic_common import build_upstream_headers
from llm_usage.capture._streaming_common import (
    STREAMING_READ_TIMEOUT_S,
    UPSTREAM_CONNECT_TIMEOUT_S,
    ErrorType,
)
from llm_usage.config import Settings
from llm_usage.core.db.session import get_session
from llm_usage.core.recording import record_event

logger = logging.getLogger(__name__)


# --- accumulator ------------------------------------------------------------


@dataclass
class AnthropicUsageAccumulator:
    """Mutable running state for one in-flight streaming response.

    Populated by `feed_event(...)` as the proxy parses SSE events on
    the side channel. Read by the writer helpers when the stream ends
    (cleanly or otherwise). Reset semantics aren't needed — one
    accumulator instance per request.
    """

    msg_id: str | None = None
    model: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0
    saw_message_start: bool = False
    saw_message_delta: bool = False
    sse_error: dict[str, object] | None = field(default=None)

    def feed_event(self, event_type: str, data_str: str) -> None:
        """Consume one SSE event and update running totals.

        Defensive against malformed payloads: a `data:` line that
        isn't valid JSON, or a `message_start` that's missing the
        expected nesting, gets silently ignored. The accumulator
        cannot raise into the byte-stream path — that would surface
        as `parse_error` in the recorder, masking the real cause.
        """
        try:
            data = json.loads(data_str)
        except json.JSONDecodeError:
            return
        if not isinstance(data, dict):
            return

        if event_type == "message_start":
            self._consume_message_start(data)
        elif event_type == "message_delta":
            self._consume_message_delta(data)
        elif event_type == "error":
            self.sse_error = data

    def _consume_message_start(self, data: dict[str, object]) -> None:
        message = data.get("message")
        if not isinstance(message, dict):
            return
        msg_id = message.get("id")
        if isinstance(msg_id, str):
            self.msg_id = msg_id
        model = message.get("model")
        if isinstance(model, str):
            self.model = model
        usage = message.get("usage")
        if isinstance(usage, dict):
            # `message_start.usage.output_tokens` is the *initial*
            # count (often 1) — intentionally skipped here so the
            # final `message_delta` value isn't pre-empted.
            self.input_tokens = int(usage.get("input_tokens") or 0)
            self.cache_write_tokens = int(usage.get("cache_creation_input_tokens") or 0)
            self.cache_read_tokens = int(usage.get("cache_read_input_tokens") or 0)
        self.saw_message_start = True

    def _consume_message_delta(self, data: dict[str, object]) -> None:
        usage = data.get("usage")
        if isinstance(usage, dict) and "output_tokens" in usage:
            self.output_tokens = int(usage.get("output_tokens") or 0)
        self.saw_message_delta = True


# --- line-buffered SSE parser -----------------------------------------------


class SSELineParser:
    """Minimal SSE parser tuned for Anthropic's wire format.

    Anthropic uses the simple SSE subset — one `event:` line and one
    `data:` line (single-line JSON) per event, separated by a blank
    line. No multi-line `data:`, no comments, no `id:`/`retry:`. The
    parser buffers across chunk boundaries (a chunk might split an
    event mid-line) and yields `(event_type, data_str)` pairs on
    completion of each event.

    Unknown fields are ignored, not errors. The proxy's job is to
    forward bytes verbatim regardless of what the parser thinks of
    them.
    """

    def __init__(self) -> None:
        self._buffer = bytearray()
        self._event_type: str | None = None
        self._data_line: str | None = None

    def feed(self, chunk: bytes) -> Iterator[tuple[str, str]]:
        """Push bytes; yield zero or more completed events."""
        self._buffer.extend(chunk)
        while True:
            nl = self._buffer.find(b"\n")
            if nl < 0:
                break
            line_bytes = bytes(self._buffer[:nl])
            del self._buffer[: nl + 1]
            # SSE allows `\r\n` line endings; strip trailing `\r`.
            line = line_bytes.rstrip(b"\r").decode("utf-8", errors="replace")
            if not line:
                # Blank line terminates an event.
                if self._event_type is not None and self._data_line is not None:
                    yield self._event_type, self._data_line
                self._event_type = None
                self._data_line = None
            elif line.startswith(":"):
                # SSE comment line — ignore.
                continue
            elif line.startswith("event:"):
                self._event_type = line[len("event:") :].lstrip()
            elif line.startswith("data:"):
                self._data_line = line[len("data:") :].lstrip()
            # `id:` / `retry:` are ignored — Anthropic doesn't use them.


# --- HTTP handler -----------------------------------------------------------


async def handle_streaming(request: Request, settings: Settings, body: bytes) -> Response:
    """Top-level orchestrator for one `stream: true` `/v1/messages` call.

    Caller (`anthropic.py:_handle_messages`) is responsible for the
    JSON pre-flight that decides streaming vs non-streaming. By the
    time we get here, `body` has already been read once and we know
    `stream: true` is in it.

    Strategy: open the upstream as a streaming response, peek at the
    status code, then split:
      - non-2xx → buffer the full body, return a plain `Response` so
        FastAPI sends it with the correct status. No row written
        (no usage data on an error envelope).
      - 2xx → hand the open response to `_iter_with_recording` and
        return a `StreamingResponse`. The generator owns the response
        lifetime; it closes the upstream connection on completion or
        cancellation.
    """
    url = f"{settings.anthropic_base_url.rstrip('/')}/v1/messages"
    headers = build_upstream_headers(request.headers, settings)

    client: httpx.AsyncClient = request.app.state.http_client
    upstream_request = client.build_request(
        "POST",
        url,
        content=body,
        headers=headers,
        timeout=httpx.Timeout(STREAMING_READ_TIMEOUT_S, connect=UPSTREAM_CONNECT_TIMEOUT_S),
    )
    started_at = time.monotonic()
    upstream_resp = await client.send(upstream_request, stream=True)

    if upstream_resp.status_code >= 300:
        # Non-2xx on a streaming request: Anthropic returns a one-shot
        # JSON error envelope, not an SSE stream. Buffer it and
        # forward; symmetric with the non-streaming non-2xx path.
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
        _iter_with_recording(upstream_resp, started_at),
        status_code=upstream_resp.status_code,
        media_type=upstream_resp.headers.get("content-type"),
    )


async def _iter_with_recording(
    upstream_resp: httpx.Response, started_at: float
) -> AsyncIterator[bytes]:
    """Generator: yield upstream bytes to client, parse usage on the side.

    Every termination path is captured. Clean completion routes
    through `_record_terminal_or_skip` (which decides success vs
    `stream_interrupted` based on whether an `event: error` was
    observed). Every exception type maps to a fixed `error_type`
    enum value and routes through `_record_failure_or_skip`. The
    upstream connection is closed in `finally` exactly once.
    """
    accumulator = AnthropicUsageAccumulator()
    parser = SSELineParser()
    try:
        async for chunk in upstream_resp.aiter_raw():
            for event_type, data_str in parser.feed(chunk):
                accumulator.feed_event(event_type, data_str)
            yield chunk
        _record_terminal_or_skip(accumulator, _duration_ms(started_at))
    except (asyncio.CancelledError, GeneratorExit):
        _record_failure_or_skip(accumulator, "client_disconnect", _duration_ms(started_at))
        raise
    except httpx.ReadTimeout:
        _record_failure_or_skip(accumulator, "timeout", _duration_ms(started_at))
    except (httpx.RemoteProtocolError, httpx.ReadError):
        _record_failure_or_skip(accumulator, "connection_dropped", _duration_ms(started_at))
    except Exception:
        logger.exception("unexpected error while proxying Anthropic stream")
        _record_failure_or_skip(accumulator, "parse_error", _duration_ms(started_at))
    finally:
        await upstream_resp.aclose()


# --- recording helpers ------------------------------------------------------


def _record_terminal_or_skip(accumulator: AnthropicUsageAccumulator, duration_ms: int) -> None:
    """Stream ended through the normal exit. Decide success vs interrupted.

    An `event: error` observed *inside* a 2xx stream is Anthropic's
    way of reporting an in-flight failure (e.g. `overloaded_error`
    after `message_start`). We treat it as `stream_interrupted` —
    distinct from `connection_dropped` (TCP-level failure) and from
    `timeout` (httpx-level read silence).
    """
    if accumulator.sse_error is not None:
        _write_event(accumulator, duration_ms, error_type="stream_interrupted")
        return
    _write_event(accumulator, duration_ms, error_type=None)


def _record_failure_or_skip(
    accumulator: AnthropicUsageAccumulator,
    error_type: ErrorType,
    duration_ms: int,
) -> None:
    """An exception killed the stream from outside SSE-protocol space."""
    _write_event(accumulator, duration_ms, error_type=error_type)


def _write_event(
    accumulator: AnthropicUsageAccumulator,
    duration_ms: int,
    error_type: ErrorType | None,
) -> None:
    """Single source of truth for writing the row. Best-effort.

    Skips entirely when we never observed `message_start` — without
    it we have no model name and no input/cache counts, so the row
    would be meaningless. The asymmetric rule from the design phase:
    *record only when we observed tokens*.

    Failure rows carry `request_id=None` so a successful retry of
    the same logical request (Anthropic always issues a fresh
    `msg_…`) can't UNIQUE-conflict with our recorded failure.
    """
    if not accumulator.saw_message_start or accumulator.model is None:
        logger.warning(
            "stream ended before message_start; skipping usage record (error_type=%s)",
            error_type,
        )
        return

    success = error_type is None
    request_id = accumulator.msg_id if success else None
    try:
        with get_session() as session:
            record_event(
                session,
                provider="anthropic",
                model=accumulator.model,
                input_tokens=accumulator.input_tokens,
                output_tokens=accumulator.output_tokens,
                cache_write_tokens=accumulator.cache_write_tokens,
                cache_read_tokens=accumulator.cache_read_tokens,
                request_id=request_id,
                duration_ms=duration_ms,
                success=success,
                error_type=error_type,
            )
            session.commit()
    except Exception:
        logger.exception(
            "failed to record Anthropic streaming event (success=%s, error_type=%s)",
            success,
            error_type,
        )


def _duration_ms(started_at: float) -> int:
    return int((time.monotonic() - started_at) * 1000)


__all__ = [
    "AnthropicUsageAccumulator",
    "ErrorType",
    "SSELineParser",
    "handle_streaming",
]
