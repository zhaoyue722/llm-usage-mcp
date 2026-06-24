"""FastAPI app factory for the local capture proxy.

The proxy is the first half of Layer 1 (`docs/spec.md`'s "Path A"):
a loopback-only HTTP server that the user's coding agent points at
via `ANTHROPIC_BASE_URL=http://127.0.0.1:5525` (or
`OPENAI_BASE_URL=http://127.0.0.1:5525/openai/v1`, and similar for
DeepSeek and Qwen). Each request gets forwarded to the real provider;
the response is parsed for the `usage` block and written to the local
`usage_events` table on the way back. The app mounts four routes:
Anthropic's native `/v1/messages` (streaming + non-streaming) and one
`/v1/chat/completions` per OpenAI-compatible provider (non-streaming
in this release; streaming is a follow-up slice).

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
from llm_usage.capture.openai_compatible import (
    OpenAICompatProvider,
)
from llm_usage.capture.openai_compatible import (
    build_router as build_openai_compatible_router,
)
from llm_usage.config import Provider, Settings, get_settings

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
    # Anthropic gets its own route shape (`/v1/messages`, no prefix)
    # because Anthropic's wire format differs from the OpenAI family.
    app.include_router(build_anthropic_router(settings))
    # OpenAI-compatible providers mount under a per-provider prefix so
    # the URL identifies the upstream (`/openai/v1/chat/completions`,
    # `/deepseek/v1/chat/completions`, `/qwen/v1/chat/completions`).
    # Users set `OPENAI_BASE_URL=http://127.0.0.1:5525/openai/v1` and
    # the SDK appends `/chat/completions`.
    for provider in _OPENAI_COMPAT_PROVIDERS:
        app.include_router(
            build_openai_compatible_router(settings, provider),
            prefix=f"/{provider}",
        )
    return app


_OPENAI_COMPAT_PROVIDERS: tuple[OpenAICompatProvider, ...] = ("openai", "deepseek", "qwen")


def run_proxy(*, port: int | None = None, log_level: str | None = None) -> None:
    """Bootstrap, warn on missing keys, run uvicorn on loopback.

    The entry point declared as `llm-usage-proxy` in `pyproject.toml`
    (via `cli.py`). Calls `bootstrap()` so a user who runs *only* the
    proxy (never the MCP server) still gets a migrated DB on first
    boot. Logs a warning for each enabled provider missing a key, but
    does *not* refuse to start — the per-request handlers return 503
    if a request hits a route whose key is unset, so a user dogfooding
    one provider doesn't have to configure four keys upfront. Finally
    `uvicorn.run` with `factory=True` so each worker imports
    `create_proxy_app` and runs the lifespan to get a fresh
    `http_client`.
    """
    import uvicorn  # local import — keeps `from llm_usage.capture import proxy` cheap

    from llm_usage.bootstrap import bootstrap

    settings = get_settings()
    logging.basicConfig(level=settings.log_level)

    # Warn before bootstrap. Alembic's `env.py` calls `fileConfig()`
    # which *replaces* root-logger handlers (per `alembic.ini`), so any
    # warning emitted after `bootstrap()` runs is invisible to a
    # pre-existing test capture handler like pytest's `caplog`. Logging
    # the missing-key state up-front is also more useful to the human
    # eyeballing the boot output — the warning lands above the wall of
    # migration `INFO` lines instead of below it.
    _warn_about_missing_keys(settings)

    bootstrap()

    bind_port = port if port is not None else settings.proxy_port
    bind_log_level = (log_level if log_level is not None else settings.log_level).lower()

    print(_build_startup_banner(settings, bind_port))

    logger.info(
        "starting capture proxy on http://%s:%d "
        "(anthropic streaming + non-streaming; openai / deepseek / qwen non-streaming)",
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


def _banner_version() -> str:
    """Installed package version, or 'unknown' from a non-installed tree."""
    from importlib import metadata

    try:
        return metadata.version("llm-usage-mcp")
    except metadata.PackageNotFoundError:
        return "unknown"


def _banner_db_path(db_url: str) -> str:
    """The on-disk DB path for display, with `$HOME` abbreviated to `~`."""
    import os

    from sqlalchemy.engine.url import make_url

    db = make_url(db_url).database or db_url
    home = os.path.expanduser("~")
    return "~" + db[len(home) :] if db.startswith(home) else db


def _build_startup_banner(settings: Settings, port: int) -> str:
    """Assemble banner data and render the watch-pom + info panel.

    Resolves the package version, the on-disk DB path, and each provider's
    key state + client base URL, then defers the actual layout to
    `cli_render.format_proxy_banner`. Color follows the same `NO_COLOR` /
    TTY rules as the CLI.
    """
    import os
    import sys

    from llm_usage.cli_render import format_proxy_banner

    pkg_version = _banner_version()
    db_path = _banner_db_path(settings.db_url)

    url = f"http://{_BIND_HOST}:{port}"
    base_url: dict[Provider, str] = {
        "anthropic": url,
        "openai": f"{url}/openai/v1",
        "deepseek": f"{url}/deepseek/v1",
        "qwen": f"{url}/qwen/v1",
    }
    order: tuple[Provider, ...] = ("anthropic", "openai", "deepseek", "qwen")
    providers: list[tuple[str, bool, str]] = [
        (p, settings.api_key_for(p) is not None, base_url[p]) for p in order
    ]

    color_enabled = not os.environ.get("NO_COLOR") and sys.stdout.isatty()
    return format_proxy_banner(
        version=pkg_version,
        host=_BIND_HOST,
        port=port,
        db_path=db_path,
        providers=providers,
        color_enabled=color_enabled,
    )


def _warn_about_missing_keys(settings: Settings) -> None:
    """Log a WARNING for each enabled provider whose API key isn't set.

    Per the design decision (Phase 3 of the OpenAI-compat slice): the
    proxy starts regardless of which keys are configured. Per-request
    handlers return 503 if a request arrives on a route whose key is
    missing. This function gives upfront visibility so the user
    doesn't discover the misconfiguration only via a confusing 503
    after they've already pointed their coding agent at the proxy.
    """
    for provider in sorted(settings.enabled_providers):
        if settings.api_key_for(provider) is None:
            logger.warning(
                "%s API key is not configured; %s requests will return 503 "
                "(set the env var and restart to enable this provider)",
                provider,
                provider,
            )


__all__ = ["create_proxy_app", "run_proxy"]
