"""Unit tests for the OpenAI-compatible streaming pieces.

`inject_include_usage`, `OpenAISSEParser`, `OpenAIUsageAccumulator` —
the pure, deterministic helpers. End-to-end behavior (bytes teed to
the client, a row written on completion) lives in
`test_capture_openai_compat_streaming_routes.py`.
"""

from __future__ import annotations

import json
from pathlib import Path

from llm_usage.capture.openai_compatible_streaming import (
    OpenAISSEParser,
    OpenAIUsageAccumulator,
    inject_include_usage,
)

_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "sample_responses"


# --- inject_include_usage --------------------------------------------------


def _decode(body: bytes) -> dict[str, object]:
    parsed = json.loads(body)
    assert isinstance(parsed, dict)
    return parsed


def test_inject_adds_stream_options_when_absent() -> None:
    """No `stream_options` at all → the proxy adds the whole object."""
    out = inject_include_usage(b'{"model":"gpt-5.2","messages":[],"stream":true}')
    assert _decode(out)["stream_options"] == {"include_usage": True}


def test_inject_adds_include_usage_into_existing_stream_options() -> None:
    """`stream_options` present but without `include_usage` → key added in place."""
    out = inject_include_usage(
        b'{"model":"gpt-5.2","stream":true,"stream_options":{"chunk_size":10}}'
    )
    stream_options = _decode(out)["stream_options"]
    assert stream_options == {"chunk_size": 10, "include_usage": True}


def test_inject_respects_explicit_true() -> None:
    """Client already set include_usage=true → body forwarded byte-identical."""
    body = b'{"model":"gpt-5.2","stream":true,"stream_options":{"include_usage":true}}'
    assert inject_include_usage(body) == body


def test_inject_respects_explicit_false() -> None:
    """Client opted out (include_usage=false) → honored, body unchanged.

    `false` is a deliberate choice; the proxy doesn't override it.
    Recording then simply skips (no usage chunk will arrive).
    """
    body = b'{"model":"gpt-5.2","stream":true,"stream_options":{"include_usage":false}}'
    assert inject_include_usage(body) == body


def test_inject_leaves_non_json_body_untouched() -> None:
    """A non-JSON body is forwarded as-is — let the upstream reject it."""
    body = b"this is not json"
    assert inject_include_usage(body) == body


def test_inject_leaves_non_object_json_untouched() -> None:
    """A JSON array / scalar at the top level isn't a chat request."""
    assert inject_include_usage(b"[1, 2, 3]") == b"[1, 2, 3]"


# --- OpenAISSEParser -------------------------------------------------------


def test_parser_yields_data_payloads() -> None:
    parser = OpenAISSEParser()
    chunk = b'data: {"a":1}\n\ndata: {"b":2}\n\n'
    assert list(parser.feed(chunk)) == ['{"a":1}', '{"b":2}']


def test_parser_skips_done_sentinel() -> None:
    """`data: [DONE]` is the stream terminator, not JSON — dropped."""
    parser = OpenAISSEParser()
    chunk = b'data: {"a":1}\n\ndata: [DONE]\n\n'
    assert list(parser.feed(chunk)) == ['{"a":1}']


def test_parser_buffers_across_chunk_boundaries() -> None:
    """A chunk can split a data line mid-byte; the buffer must hold."""
    parser = OpenAISSEParser()
    payloads: list[str] = []
    for chunk in [b'data: {"a"', b":1}\n", b"\ndata: [DO", b"NE]\n\n"]:
        payloads.extend(parser.feed(chunk))
    assert payloads == ['{"a":1}']


def test_parser_handles_crlf() -> None:
    parser = OpenAISSEParser()
    assert list(parser.feed(b'data: {"a":1}\r\n\r\n')) == ['{"a":1}']


def test_parser_ignores_comments_and_blank_lines() -> None:
    parser = OpenAISSEParser()
    chunk = b': keep-alive\n\ndata: {"a":1}\n\n'
    assert list(parser.feed(chunk)) == ['{"a":1}']


# --- OpenAIUsageAccumulator ------------------------------------------------


def _feed_fixture(parser: OpenAISSEParser, acc: OpenAIUsageAccumulator, name: str) -> None:
    for payload in parser.feed((_FIXTURE_DIR / name).read_bytes()):
        acc.feed(payload)


def test_accumulator_captures_the_usage_chunk() -> None:
    """The terminal `choices:[]`+`usage` chunk is the one kept."""
    parser = OpenAISSEParser()
    acc = OpenAIUsageAccumulator()
    _feed_fixture(parser, acc, "openai_chat_completions_stream_ok.sse")

    assert acc.usage_chunk is not None
    assert acc.usage_chunk["id"] == "chatcmpl-streamopenai123"
    usage = acc.usage_chunk["usage"]
    assert isinstance(usage, dict)
    assert usage["prompt_tokens"] == 20


def test_accumulator_none_when_no_usage_chunk() -> None:
    """A stream without the usage chunk (client opted out) → usage_chunk stays None."""
    parser = OpenAISSEParser()
    acc = OpenAIUsageAccumulator()
    _feed_fixture(parser, acc, "openai_chat_completions_stream_no_usage.sse")
    assert acc.usage_chunk is None


def test_accumulator_ignores_pre_usage_content_chunks() -> None:
    """Content delta chunks carry no `usage` — they must not be captured."""
    acc = OpenAIUsageAccumulator()
    acc.feed('{"id":"x","model":"m","choices":[{"delta":{"content":"hi"}}]}')
    assert acc.usage_chunk is None


def test_accumulator_ignores_null_and_empty_usage() -> None:
    """Some chunks carry `usage: null` or `usage: {}` — not the terminal chunk."""
    acc = OpenAIUsageAccumulator()
    acc.feed('{"id":"x","model":"m","choices":[],"usage":null}')
    acc.feed('{"id":"x","model":"m","choices":[],"usage":{}}')
    assert acc.usage_chunk is None


def test_accumulator_ignores_malformed_json() -> None:
    """A non-JSON `data:` payload must not raise into the byte path."""
    acc = OpenAIUsageAccumulator()
    acc.feed("not json at all")
    acc.feed('{"unterminated":')
    assert acc.usage_chunk is None
