"""Unit tests for `capture/anthropic.py`'s pure helpers.

Side-effect-free pieces (`build_upstream_headers`, `_response_to_event_args`,
`_safe_parse_json`) get tested here without a FastAPI app or a DB. The
end-to-end behavior (record_event called with the right args, header
rewrite landing in the upstream request, streaming dispatch) lives in
`test_capture_proxy.py`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import SecretStr

from llm_usage.capture._anthropic_common import build_upstream_headers
from llm_usage.capture.anthropic import (
    _response_to_event_args,
    _safe_parse_json,
)

_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "sample_responses"


@pytest.fixture
def key() -> SecretStr:
    """A `SecretStr` matching what `Settings.api_key_for("anthropic")` returns."""
    return SecretStr("sk-real-server")


# --- build_upstream_headers ----------------------------------------------


def test_build_upstream_headers_sets_server_side_key_only(key: SecretStr) -> None:
    """Client-provided x-api-key / Authorization must NOT survive the rewrite."""
    headers = build_upstream_headers(
        {
            "x-api-key": "sk-client-pretend",
            "authorization": "Bearer client-junk",
            "user-agent": "MyAgent/1.0",
        },
        key,
    )
    assert headers["x-api-key"] == "sk-real-server"
    # Nothing client-side leaks through — whitelist semantics.
    assert "authorization" not in {k.lower() for k in headers}
    assert "user-agent" not in {k.lower() for k in headers}


def test_build_upstream_headers_forwards_anthropic_version_when_set(key: SecretStr) -> None:
    headers = build_upstream_headers({"anthropic-version": "2024-10-22"}, key)
    assert headers["anthropic-version"] == "2024-10-22"


def test_build_upstream_headers_injects_default_anthropic_version(key: SecretStr) -> None:
    """No client-supplied version -> default to the documented stable value."""
    headers = build_upstream_headers({}, key)
    assert headers["anthropic-version"] == "2023-06-01"


def test_build_upstream_headers_passes_anthropic_beta_through(key: SecretStr) -> None:
    headers = build_upstream_headers(
        {"anthropic-beta": "prompt-caching-2024-07-31,tools-2024-04-04"},
        key,
    )
    assert headers["anthropic-beta"] == "prompt-caching-2024-07-31,tools-2024-04-04"


def test_build_upstream_headers_omits_beta_when_absent(key: SecretStr) -> None:
    headers = build_upstream_headers({}, key)
    assert "anthropic-beta" not in headers


def test_build_upstream_headers_always_sets_content_type_json(key: SecretStr) -> None:
    headers = build_upstream_headers({"content-type": "text/plain"}, key)
    assert headers["content-type"] == "application/json"


# --- _safe_parse_json ------------------------------------------------------


def test_safe_parse_json_returns_object_for_valid_input() -> None:
    assert _safe_parse_json(b'{"stream": true, "model": "x"}') == {"stream": True, "model": "x"}


def test_safe_parse_json_returns_none_for_invalid_json() -> None:
    assert _safe_parse_json(b"not json at all") is None


def test_safe_parse_json_returns_none_for_non_object() -> None:
    """A JSON array or scalar at the top level isn't useful for our check."""
    assert _safe_parse_json(b"[1, 2, 3]") is None
    assert _safe_parse_json(b'"just a string"') is None


def test_safe_parse_json_returns_none_for_empty_body() -> None:
    assert _safe_parse_json(b"") is None


# --- _response_to_event_args ----------------------------------------------


def test_response_to_event_args_maps_full_payload() -> None:
    """Use the real-shape fixture to pin every field."""
    data = json.loads((_FIXTURE_DIR / "anthropic_messages_ok.json").read_text())
    kwargs = _response_to_event_args(data, duration_ms=1234)
    assert kwargs is not None
    assert kwargs["provider"] == "anthropic"
    assert kwargs["model"] == "claude-sonnet-4-6"
    assert kwargs["input_tokens"] == 100
    assert kwargs["output_tokens"] == 50
    assert kwargs["cache_write_tokens"] == 10
    assert kwargs["cache_read_tokens"] == 5
    assert kwargs["request_id"] == "msg_01ABCDEF1234567890abcdef"
    assert kwargs["duration_ms"] == 1234
    assert kwargs["success"] is True


def test_response_to_event_args_defaults_cache_tokens_to_zero() -> None:
    """Models without prompt caching omit `cache_*_input_tokens` entirely."""
    kwargs = _response_to_event_args(
        {
            "id": "msg_xyz",
            "model": "claude-haiku-4-5",
            "usage": {"input_tokens": 42, "output_tokens": 7},
        },
        duration_ms=0,
    )
    assert kwargs is not None
    assert kwargs["cache_write_tokens"] == 0
    assert kwargs["cache_read_tokens"] == 0


@pytest.mark.parametrize(
    "data",
    [
        pytest.param({"model": "x", "usage": {}}, id="no_id"),
        pytest.param({"id": "msg_x", "usage": {}}, id="no_model"),
        pytest.param({"id": "msg_x", "model": "x"}, id="no_usage"),
        pytest.param({"id": 123, "model": "x", "usage": {}}, id="id_not_string"),
        pytest.param({"id": "msg_x", "model": "x", "usage": "not_a_dict"}, id="usage_not_dict"),
    ],
)
def test_response_to_event_args_returns_none_on_shape_mismatch(data: dict[str, object]) -> None:
    """A shape mismatch surfaces as `None`; caller logs+skips, doesn't crash."""
    assert _response_to_event_args(data, duration_ms=0) is None
