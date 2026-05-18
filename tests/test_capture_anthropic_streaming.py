"""Unit tests for the streaming pieces: `SSELineParser` + `AnthropicUsageAccumulator`.

End-to-end behavior (the proxy actually streaming bytes to a client
and writing rows when the stream ends) lives in
`test_capture_proxy.py`. This file pins the small, deterministic
pieces in isolation.
"""

from __future__ import annotations

from pathlib import Path

from llm_usage.capture.anthropic_streaming import (
    AnthropicUsageAccumulator,
    SSELineParser,
)

_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "sample_responses"


# --- SSELineParser ---------------------------------------------------------


def test_parser_yields_events_from_single_well_formed_chunk() -> None:
    parser = SSELineParser()
    chunk = b'event: foo\ndata: {"a":1}\n\nevent: bar\ndata: {"b":2}\n\n'
    assert list(parser.feed(chunk)) == [("foo", '{"a":1}'), ("bar", '{"b":2}')]


def test_parser_buffers_across_chunk_boundaries() -> None:
    """Chunks may split a line — even mid-byte. Buffer must hold."""
    parser = SSELineParser()
    events: list[tuple[str, str]] = []
    # The same event split into three chunks, with a split inside the
    # JSON payload (between `1` and `,`) and inside the trailing `\n\n`.
    for chunk in [b"event: foo\nda", b'ta: {"a":1', b"}\n\n"]:
        events.extend(parser.feed(chunk))
    assert events == [("foo", '{"a":1}')]


def test_parser_handles_crlf_line_endings() -> None:
    parser = SSELineParser()
    chunk = b'event: foo\r\ndata: {"a":1}\r\n\r\n'
    assert list(parser.feed(chunk)) == [("foo", '{"a":1}')]


def test_parser_ignores_comments_and_unknown_fields() -> None:
    """`:` is an SSE comment; `id:` / `retry:` aren't fields Anthropic uses."""
    parser = SSELineParser()
    chunk = b": keep-alive\nid: 7\nretry: 5000\nevent: foo\ndata: {}\n\n"
    assert list(parser.feed(chunk)) == [("foo", "{}")]


def test_parser_drops_partial_event_without_blank_terminator() -> None:
    """No blank line == event not complete == nothing emitted (yet)."""
    parser = SSELineParser()
    assert list(parser.feed(b'event: foo\ndata: {"a":1}\n')) == []


def test_parser_drops_event_missing_data_line() -> None:
    """An `event:` without a paired `data:` is incomplete; no event emitted."""
    parser = SSELineParser()
    assert list(parser.feed(b"event: foo\n\n")) == []


def test_parser_handles_ping_pass_through_naturally() -> None:
    """`ping` events are valid SSE — emitted by the parser, ignored downstream."""
    parser = SSELineParser()
    chunk = b'event: ping\ndata: {"type":"ping"}\n\n'
    assert list(parser.feed(chunk)) == [("ping", '{"type":"ping"}')]


# --- AnthropicUsageAccumulator --------------------------------------------


def _feed_fixture(parser: SSELineParser, acc: AnthropicUsageAccumulator, path: Path) -> None:
    """Read an .sse fixture and feed it through parser → accumulator."""
    for event_type, data_str in parser.feed(path.read_bytes()):
        acc.feed_event(event_type, data_str)


def test_accumulator_happy_path_from_fixture() -> None:
    """Full stream → input/cache from `message_start`, output from `message_delta`."""
    parser = SSELineParser()
    acc = AnthropicUsageAccumulator()
    _feed_fixture(parser, acc, _FIXTURE_DIR / "anthropic_messages_stream_ok.sse")

    assert acc.msg_id == "msg_streamtest_ok_123"
    assert acc.model == "claude-sonnet-4-6"
    assert acc.input_tokens == 100
    assert acc.output_tokens == 50  # message_delta's 50, NOT message_start's initial 1
    assert acc.cache_write_tokens == 10
    assert acc.cache_read_tokens == 5
    assert acc.saw_message_start is True
    assert acc.saw_message_delta is True
    assert acc.sse_error is None


def test_accumulator_records_sse_error_from_mid_stream_event() -> None:
    """An `event: error` after `message_start` populates `sse_error`."""
    parser = SSELineParser()
    acc = AnthropicUsageAccumulator()
    _feed_fixture(parser, acc, _FIXTURE_DIR / "anthropic_messages_stream_error_mid.sse")

    assert acc.saw_message_start is True
    assert acc.saw_message_delta is False  # never got there
    assert acc.input_tokens == 80
    assert acc.output_tokens == 0  # no message_delta seen — stays at 0
    assert acc.sse_error is not None
    error = acc.sse_error.get("error")
    assert isinstance(error, dict)
    assert error.get("type") == "overloaded_error"


def test_accumulator_partial_stream_keeps_message_start_state() -> None:
    """A truncated stream still leaves the message_start fields populated."""
    parser = SSELineParser()
    acc = AnthropicUsageAccumulator()
    _feed_fixture(parser, acc, _FIXTURE_DIR / "anthropic_messages_stream_only_message_start.sse")

    assert acc.saw_message_start is True
    assert acc.saw_message_delta is False
    assert acc.msg_id == "msg_streamtest_partial_789"
    assert acc.model == "claude-sonnet-4-6"
    assert acc.input_tokens == 42
    assert acc.output_tokens == 0  # the initial `1` from message_start is intentionally dropped


def test_accumulator_does_not_pre_empt_output_from_message_start() -> None:
    """`message_start.usage.output_tokens` (initial 1) must NOT seed output_tokens.

    Anthropic's protocol: `message_delta` carries the final cumulative
    value. Trusting `message_start`'s `1` would silently underreport
    almost every successful stream.
    """
    acc = AnthropicUsageAccumulator()
    acc.feed_event(
        "message_start",
        '{"type":"message_start","message":{"id":"msg_x","model":"m","usage":'
        '{"input_tokens":50,"output_tokens":1,"cache_creation_input_tokens":0,'
        '"cache_read_input_tokens":0}}}',
    )
    assert acc.output_tokens == 0


def test_accumulator_silently_ignores_malformed_json() -> None:
    """A `data:` payload that isn't JSON shouldn't raise into the byte path."""
    acc = AnthropicUsageAccumulator()
    acc.feed_event("message_start", "not-json")
    acc.feed_event("message_delta", "{also-not-json")
    # Nothing populated, nothing raised.
    assert acc.saw_message_start is False
    assert acc.saw_message_delta is False


def test_accumulator_silently_ignores_unexpected_shapes() -> None:
    """A top-level scalar or list payload is treated as ignorable."""
    acc = AnthropicUsageAccumulator()
    acc.feed_event("message_start", '"a string"')
    acc.feed_event("message_start", "[1, 2, 3]")
    assert acc.saw_message_start is False


def test_accumulator_treats_missing_cache_fields_as_zero() -> None:
    """Older / non-cache models omit `cache_*_input_tokens` entirely."""
    acc = AnthropicUsageAccumulator()
    acc.feed_event(
        "message_start",
        '{"message":{"id":"msg_x","model":"m","usage":{"input_tokens":7,"output_tokens":1}}}',
    )
    assert acc.input_tokens == 7
    assert acc.cache_write_tokens == 0
    assert acc.cache_read_tokens == 0


def test_accumulator_treats_null_cache_fields_as_zero() -> None:
    """Some Anthropic responses send `null` explicitly for cache fields.

    Defensive parsing: `int(usage.get("...") or 0)` treats both
    missing-key and explicit-null the same way. Pinning this so a
    future refactor that switches to `int(usage.get("...", 0))` —
    which would raise on null — doesn't slip through.
    """
    acc = AnthropicUsageAccumulator()
    acc.feed_event(
        "message_start",
        '{"message":{"id":"msg_x","model":"m","usage":'
        '{"input_tokens":12,"output_tokens":1,'
        '"cache_creation_input_tokens":null,"cache_read_input_tokens":null}}}',
    )
    assert acc.input_tokens == 12
    assert acc.cache_write_tokens == 0
    assert acc.cache_read_tokens == 0


def test_accumulator_message_delta_without_output_tokens_is_noop_for_count() -> None:
    """A `message_delta` missing `usage.output_tokens` shouldn't zero it out."""
    acc = AnthropicUsageAccumulator()
    # Pre-populate via a real message_start, then feed a stripped message_delta.
    acc.feed_event(
        "message_start",
        '{"message":{"id":"x","model":"m","usage":{"input_tokens":1,"output_tokens":1}}}',
    )
    acc.feed_event("message_delta", '{"delta":{"stop_reason":"end_turn"}}')
    assert acc.output_tokens == 0  # never got a real value; stays at default
    assert acc.saw_message_delta is True  # but we did see the event
