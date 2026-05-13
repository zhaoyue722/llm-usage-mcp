"""Smoke test: `main()` dispatches to `bootstrap()` then `server.run()`."""

from __future__ import annotations

import pytest

import llm_usage
from llm_usage.mcp.server import server


def test_main_calls_bootstrap_then_serves(monkeypatch: pytest.MonkeyPatch) -> None:
    """`main()` must run bootstrap first, then hand off to the FastMCP server.

    Both calls land in `calls` so the *order* is asserted, not just that
    each happened. `bootstrap` is patched as an attribute on the
    `llm_usage` package, which is the lookup `main()` performs (it's
    defined in the same module).
    """
    calls: list[str] = []
    monkeypatch.setattr("llm_usage.bootstrap", lambda: calls.append("bootstrap"))
    monkeypatch.setattr(server, "run", lambda: calls.append("server.run"))

    llm_usage.main()

    assert calls == ["bootstrap", "server.run"]
