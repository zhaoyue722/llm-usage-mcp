"""Shared helpers for the OpenAI-compatible capture modules.

Three providers (OpenAI itself, DeepSeek, Qwen / DashScope) all serve
the same `/chat/completions` wire format with the same auth shape
(`Authorization: Bearer …`) and the same envelope-style errors. They
differ on the response's `usage` block (DeepSeek splits cache tokens
into `prompt_cache_hit_tokens` / `prompt_cache_miss_tokens`; OpenAI
uses `prompt_tokens_details.cached_tokens`; Qwen often omits cache
fields entirely) — that variance is isolated in
`openai_compatible.py`'s per-provider token extractors. Everything
else lives here so the three sibling routers don't drift.

The underscore prefix marks the module as package-internal — there's
no public API and nothing outside `capture/` should import from this
file. Same convention as `_anthropic_common.py`.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, Final

from pydantic import SecretStr

# Returned verbatim as the 400 body when a client sends `stream: true`
# in Phase 1 of this module. Shape matches OpenAI's own
# `invalid_request_error` envelope so a well-behaved client parses
# this the same way it'd parse a real upstream rejection. Streaming
# support lands in a follow-up slice.
STREAM_REJECTION_BODY: Final[dict[str, Any]] = {
    "error": {
        "message": (
            "Streaming is not yet supported by the local capture proxy "
            "for OpenAI-compatible providers. Set stream: false to "
            "record usage through this proxy, or call the upstream "
            "directly while streaming support lands in a follow-up "
            "release."
        ),
        "type": "invalid_request_error",
        "param": None,
        "code": None,
    },
}


# Returned as the 503 body when the client hits a provider's route
# but its API key isn't configured. 503 (rather than 401) because the
# server is misconfigured, not the client unauthorized — and the
# message says exactly which env var to set so the fix is obvious.
def missing_key_envelope(env_var_name: str) -> dict[str, Any]:
    return {
        "error": {
            "message": (
                f"{env_var_name} is not set on the proxy server. The "
                "capture proxy holds the upstream API key server-side; "
                "set the env var and restart the proxy to use this "
                "provider."
            ),
            "type": "configuration_error",
            "param": None,
            "code": None,
        },
    }


def build_upstream_headers(key: SecretStr) -> dict[str, str]:
    """Whitelist + rewrite headers for the upstream `/chat/completions` POST.

    Same posture as the Anthropic builder: drop every client header,
    write `Authorization: Bearer …` and `content-type: application/json`
    ourselves. The client never gets to forward its own auth — the
    proxy holds the key. No `User-Agent` propagation either; httpx's
    default is fine and forwarding the client's UA leaks coding-agent
    identity to the provider for no benefit.
    """
    return {
        "Authorization": f"Bearer {key.get_secret_value()}",
        "content-type": "application/json",
    }


def safe_parse_json(body: bytes) -> dict[str, Any] | None:
    """Best-effort JSON parse for the `stream` pre-flight.

    A non-JSON or non-object body is fine — we let the upstream return
    its own validation error rather than try to second-guess. Only
    used for the `stream: true` check; forwarding uses the raw `body`
    bytes so we don't re-serialize and drift from what the client
    sent.
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


def header_lookup(headers: Mapping[str, str], name: str) -> str | None:
    """Case-insensitive header lookup, returns None if absent.

    FastAPI's `Request.headers` is already case-insensitive on
    `__getitem__`, but the `Mapping[str, str]` we accept (for
    testability) might not be — this normalizes.
    """
    name_lower = name.lower()
    for k, v in headers.items():
        if k.lower() == name_lower:
            return v
    return None


__all__ = [
    "STREAM_REJECTION_BODY",
    "build_upstream_headers",
    "header_lookup",
    "missing_key_envelope",
    "safe_parse_json",
]
