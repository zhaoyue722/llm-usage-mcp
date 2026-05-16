"""FastAPI app factory for the local capture proxy.

The proxy is the first half of Layer 1 (`docs/spec.md`'s "Path A"):
a loopback-only HTTP server that the user's coding agent points at
via `ANTHROPIC_BASE_URL=http://127.0.0.1:5525`. Each request gets
forwarded to the real provider; the response is parsed for the
`usage` block and written to the local `usage_events` table on the
way back. Phase 1 mounts only the Anthropic `/v1/messages` route;
subsequent phases bolt OpenAI-compatible routes onto the same app.

Two pieces of public surface:

- `create_proxy_app(settings)` — the FastAPI app factory. Used by
  uvicorn's `factory=True` mode and by tests (which inject a tmp-path
  DB and a pre-seeded `http_client` instead of relying on the
  lifespan).
- `run_proxy(*, port, log_level)` — the CLI entry point. Bootstraps
  the DB, demands the Anthropic key, and binds uvicorn to
  `127.0.0.1` — never `0.0.0.0`. Loopback is non-negotiable: a
  "local-first, privacy is a feature" product can't ship a process
  that the local network can connect to.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from llm_usage.capture.anthropic import build_router as build_anthropic_router
from llm_usage.config import Settings, get_settings

logger = logging.getLogger(__name__)

# Always bind to loopback — never the network. Documented in
# `.env.example` and `docs/configuration.md`; constant rather than a
# flag so a misconfiguration can't silently expose the proxy.
_BIND_HOST = "127.0.0.1"

# Connect timeout intentionally short (10s) so a wedged DNS / network
# fails fast; read timeout generous (60s) because long completions
# legitimately take that long upstream.
_UPSTREAM_CONNECT_TIMEOUT_S = 10.0
_UPSTREAM_READ_TIMEOUT_S = 60.0


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Manage one pooled `httpx.AsyncClient` per process.

    Connection pooling matters: the proxy will issue many sequential
    Anthropic calls from the same process; reusing one client preserves
    keepalive and avoids a TLS handshake per request. Tests bypass the
    lifespan (it isn't invoked by `httpx.ASGITransport`) and inject
    `app.state.http_client` directly.
    """
    client = httpx.AsyncClient(
        timeout=httpx.Timeout(_UPSTREAM_READ_TIMEOUT_S, connect=_UPSTREAM_CONNECT_TIMEOUT_S),
    )
    app.state.http_client = client
    try:
        yield
    finally:
        await client.aclose()


def create_proxy_app(settings: Settings | None = None) -> FastAPI:
    """Build the FastAPI app. Default factory used by uvicorn.

    `settings=None` defaults to `get_settings()` so `uvicorn.run(...,
    factory=True)` can call this with no arguments. Tests pass an
    explicit `Settings` constructed against a tmp-path DB.
    """
    if settings is None:
        settings = get_settings()
    app = FastAPI(title="llm-usage capture proxy", lifespan=_lifespan)
    app.include_router(build_anthropic_router(settings))
    return app


def run_proxy(*, port: int | None = None, log_level: str | None = None) -> None:
    """Bootstrap, gate on Anthropic key, run uvicorn on loopback.

    The entry point declared as `llm-usage-proxy` in `pyproject.toml`
    (via `cli.py`). Calls `bootstrap()` so a user who runs *only* the
    proxy (never the MCP server) still gets a migrated DB on first
    boot. Then `Settings.require_keys({"anthropic"})` — Phase 1 serves
    only Anthropic, so demanding OpenAI / Qwen / DeepSeek keys would
    be hostile to a user who hasn't set them yet. Finally `uvicorn.run`
    with `factory=True` so each worker imports `create_proxy_app` and
    runs the lifespan to get a fresh `http_client`.
    """
    import uvicorn  # local import — keeps `from llm_usage.capture import proxy` cheap

    from llm_usage.bootstrap import bootstrap

    settings = get_settings()
    logging.basicConfig(level=settings.log_level)

    bootstrap()
    settings.require_keys({"anthropic"})

    bind_port = port if port is not None else settings.proxy_port
    bind_log_level = (log_level if log_level is not None else settings.log_level).lower()

    logger.info(
        "starting capture proxy on http://%s:%d (anthropic only, non-streaming)",
        _BIND_HOST,
        bind_port,
    )
    uvicorn.run(
        "llm_usage.capture.proxy:create_proxy_app",
        factory=True,
        host=_BIND_HOST,
        port=bind_port,
        log_level=bind_log_level,
    )


__all__ = ["create_proxy_app", "run_proxy"]
