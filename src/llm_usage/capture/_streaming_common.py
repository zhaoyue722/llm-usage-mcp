"""Shared primitives for the streaming capture modules.

`anthropic_streaming.py` and `openai_compatible_streaming.py` are two
separate handlers (the Anthropic SSE wire format differs from the
OpenAI family's), but they share two provider-neutral pieces:

- `ErrorType` — the closed set of `error_type` values a streaming
  capture can attribute a failure to. Provider-neutral by design: a
  TCP drop is a TCP drop whether the upstream is Anthropic or
  DeepSeek, and a `GROUP BY error_type` query over `usage_events`
  stays meaningful only if every streaming module draws from the
  same vocabulary.
- The two upstream timeout constants — streaming reads legitimately
  go quiet for tens of seconds (tool use, extended thinking), so the
  read timeout is far more generous than the non-streaming default.

(The elapsed-millis helper is *not* shared: each module keeps a
one-line `_duration_ms`, because a shared `duration_ms` symbol would
shadow the `duration_ms` parameter the recording helpers already
carry — a cosmetic collision not worth a cross-module import.)

The underscore prefix marks this module package-internal; nothing
outside `capture/` should import from it.
"""

from __future__ import annotations

from typing import Final, Literal

# All possible `error_type` values a streaming capture path can write
# (Anthropic) or log (OpenAI family). Closed `Literal` so static
# checking catches typos and a reader can grep every producer.
#
#   stream_interrupted  in-band error event inside a 2xx stream
#                       (Anthropic `event: error`)
#   upstream_error      reserved — a non-2xx that still writes a row
#   client_disconnect   the proxy's response generator was cancelled
#                       (client closed the connection) / GeneratorExit
#   connection_dropped  upstream TCP failure (httpx RemoteProtocolError
#                       / ReadError) mid-stream
#   timeout             httpx ReadTimeout — upstream went silent past
#                       the streaming read budget
#   parse_error         an unexpected exception in the parser path
ErrorType = Literal[
    "stream_interrupted",
    "upstream_error",
    "client_disconnect",
    "connection_dropped",
    "timeout",
    "parse_error",
]

# Streaming reads can be quiet for tens of seconds (tool use,
# extended thinking) without being broken. 60s — the non-streaming
# default — is tight enough that legitimate slow completions would
# trip it; 300s keeps a bound so a wedged upstream still escapes.
STREAMING_READ_TIMEOUT_S: Final[float] = 300.0
UPSTREAM_CONNECT_TIMEOUT_S: Final[float] = 10.0


__all__ = [
    "STREAMING_READ_TIMEOUT_S",
    "UPSTREAM_CONNECT_TIMEOUT_S",
    "ErrorType",
]
