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
from typing import Final

from llm_usage.config import Settings

# Default value injected when the client doesn't pass an
# `anthropic-version` header. Matches Anthropic's documented stable
# version; the proxy forwards whatever the client sent if they did.
ANTHROPIC_VERSION_DEFAULT: Final[str] = "2023-06-01"


def build_upstream_headers(
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
        "anthropic-version": client_headers.get("anthropic-version", ANTHROPIC_VERSION_DEFAULT),
        "content-type": "application/json",
    }
    if (beta := client_headers.get("anthropic-beta")) is not None:
        headers["anthropic-beta"] = beta
    return headers


__all__ = ["ANTHROPIC_VERSION_DEFAULT", "build_upstream_headers"]
