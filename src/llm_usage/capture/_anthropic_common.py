"""Shared helpers for the Anthropic capture modules.

`anthropic.py` (orchestrator + non-streaming path) and
`anthropic_streaming.py` (streaming path) both need to rebuild the
upstream request headers from scratch. Putting that helper in either
module would create a circular import (the orchestrator already
imports the streaming handler at module-load time), so this leaf
module owns the shared symbols.

The underscore prefix marks it as package-internal — there's no
public API here and nothing outside `capture/anthropic*` should
import from this file.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from pydantic import SecretStr

# Default value injected when the client doesn't pass an
# `anthropic-version` header. Matches Anthropic's documented stable
# version; the proxy forwards whatever the client sent if they did.
ANTHROPIC_VERSION_DEFAULT: Final[str] = "2023-06-01"

# Body returned as the 503 when `/v1/messages` is hit but the proxy
# has no `ANTHROPIC_API_KEY` configured. Shape matches Anthropic's
# own error envelope (`{"type":"error","error":{...}}`) so a
# well-behaved client parses it the same way it'd parse a real
# upstream rejection. 503 (rather than 401) because the *server* is
# misconfigured, not the *client* unauthorized — and the message
# names the exact env var to set.
MISSING_KEY_ENVELOPE: Final[dict[str, Any]] = {
    "type": "error",
    "error": {
        "type": "configuration_error",
        "message": (
            "ANTHROPIC_API_KEY is not set on the proxy server. The "
            "capture proxy holds the upstream API key server-side; "
            "set the env var and restart the proxy to use this "
            "provider."
        ),
    },
}


def build_upstream_headers(
    client_headers: Mapping[str, str],
    key: SecretStr,
) -> dict[str, str]:
    """Whitelist client headers; always overwrite `x-api-key` server-side.

    Anthropic authenticates via `x-api-key`, not `Authorization: Bearer`.
    The proxy never forwards the client's auth — the user's coding
    agent doesn't need to know the API key, the proxy holds it.

    `key` is passed in already resolved (rather than looked up from
    `Settings` in here) so the caller can return a clean 503 if it's
    missing instead of crashing inside the header builder. Mirrors the
    OpenAI-compatible sibling's design.
    """
    headers: dict[str, str] = {
        "x-api-key": key.get_secret_value(),
        "anthropic-version": client_headers.get("anthropic-version", ANTHROPIC_VERSION_DEFAULT),
        "content-type": "application/json",
    }
    if (beta := client_headers.get("anthropic-beta")) is not None:
        headers["anthropic-beta"] = beta
    return headers


__all__ = [
    "ANTHROPIC_VERSION_DEFAULT",
    "MISSING_KEY_ENVELOPE",
    "build_upstream_headers",
]
