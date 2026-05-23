"""End-to-end tests for the capture proxy.

Drives the FastAPI app via `httpx.ASGITransport` (no real network bind,
no real port) and mocks the upstream `api.anthropic.com` with respx.
Two transports stacked: the test's outer client uses ASGITransport to
reach the proxy; the proxy's inner client uses respx (which patches
`httpx.AsyncClient` globally) to reach the "real" Anthropic.

Test pattern: each test spins up the FastAPI app, sets up its respx
route, makes the call via `asyncio.run`, then asserts both the
forwarded response shape and the row(s) written to `usage_events`. No
pytest-asyncio dep — the existing codebase convention is `asyncio.run`
inside each test, matched here.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from sqlalchemy import select

from llm_usage.bootstrap import migrate_to_head
from llm_usage.capture.proxy import create_proxy_app
from llm_usage.config import Settings
from llm_usage.core.db.models import UsageEvent
from llm_usage.core.db.session import get_session
from llm_usage.core.pricing import Pricing, upsert_pricing

_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "sample_responses"
_OK_RESPONSE: dict[str, Any] = json.loads((_FIXTURE_DIR / "anthropic_messages_ok.json").read_text())
_UPSTREAM_URL = "https://api.anthropic.com/v1/messages"


@pytest.fixture
def settings_with_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    """Fresh DB seeded with pricing + a server-side Anthropic key in env.

    Also strips ambient `*_proxy` env vars: a developer running with a
    corporate / SOCKS proxy in their shell would otherwise have httpx
    try to route the (mocked) upstream call through it, which both
    defeats respx's interception and can fail outright when SOCKS
    dependencies aren't installed.
    """
    monkeypatch.setenv("LLM_USAGE_DB_URL", f"sqlite:///{tmp_path / 'usage.db'}")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-server-key")
    monkeypatch.setenv("LLM_USAGE_ANTHROPIC_BASE_URL", "https://api.anthropic.com")
    for var in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "NO_PROXY",
        "no_proxy",
    ):
        monkeypatch.delenv(var, raising=False)
    migrate_to_head()
    with get_session() as session:
        upsert_pricing(
            session,
            [
                # Approximate Anthropic rates for the fixture model. Cache
                # rates are seeded because the fixture response includes
                # nonzero cache_creation_input_tokens / cache_read_input_tokens;
                # without them, CostCalculator would raise and the
                # best-effort recorder would (correctly) drop the event.
                Pricing(
                    "anthropic",
                    "claude-sonnet-4-6",
                    input_per_million_usd=3.0,
                    output_per_million_usd=15.0,
                    cache_write_per_million_usd=3.75,
                    cache_read_per_million_usd=0.30,
                    fetched_at=1,
                ),
            ],
        )
        session.commit()
    return Settings()


@pytest.fixture
def proxy_app(settings_with_db: Settings) -> Iterator[Any]:
    """Build the FastAPI app and seed `state.http_client` directly.

    `httpx.ASGITransport` doesn't trigger FastAPI's lifespan handler, so
    the test bypasses the real connection-pooled client and injects a
    fresh one. The client gets closed after the test.
    """
    app = create_proxy_app(settings_with_db)
    app.state.http_client = httpx.AsyncClient(timeout=30.0)
    try:
        yield app
    finally:
        asyncio.run(app.state.http_client.aclose())


def _post(
    app: Any,
    body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    """Synchronous-looking helper that drives the ASGI app."""

    async def call() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://proxy") as client:
            return await client.post(
                "/v1/messages",
                json=body or {"model": "claude-sonnet-4-6", "messages": []},
                headers=headers or {},
            )

    return asyncio.run(call())


# --- forwarding + recording ------------------------------------------------


@respx.mock
def test_messages_forwards_response_unchanged_and_records_usage(proxy_app: Any) -> None:
    """The happy path: 2xx upstream -> client gets the body, event lands."""
    respx.post(_UPSTREAM_URL).mock(return_value=httpx.Response(200, json=_OK_RESPONSE))

    response = _post(proxy_app)

    assert response.status_code == 200
    assert response.json() == _OK_RESPONSE  # body forwarded verbatim

    with get_session() as session:
        events = session.scalars(select(UsageEvent)).all()
    assert len(events) == 1
    event = events[0]
    assert event.provider == "anthropic"
    assert event.model == "claude-sonnet-4-6"
    assert event.input_tokens == 100
    assert event.output_tokens == 50
    assert event.cache_write_tokens == 10
    assert event.cache_read_tokens == 5
    assert event.request_id == "msg_01ABCDEF1234567890abcdef"
    assert event.success is True
    # Cost snapshotted at insert. Fixture seeds (3, 15, 3.75, 0.30) per M;
    # tokens are (100, 50, 10, 5) → 100*3 + 50*15 + 10*3.75 + 5*0.30 = 1089.0
    # USD-per-million-tokens. nano = round(1089 * 1000) = 1_089_000.
    assert event.cost_nano_usd == 1_089_000


@respx.mock
def test_idempotency_via_anthropic_message_id(proxy_app: Any) -> None:
    """Two identical responses (same `id`) yield exactly one row."""
    respx.post(_UPSTREAM_URL).mock(return_value=httpx.Response(200, json=_OK_RESPONSE))

    _post(proxy_app)
    _post(proxy_app)

    with get_session() as session:
        events = session.scalars(select(UsageEvent)).all()
    assert len(events) == 1


# --- header rewrite --------------------------------------------------------


@respx.mock
def test_upstream_sees_server_side_api_key_not_client_auth(proxy_app: Any) -> None:
    """The client's Authorization / x-api-key MUST NOT leak upstream."""
    route = respx.post(_UPSTREAM_URL).mock(return_value=httpx.Response(200, json=_OK_RESPONSE))

    _post(
        proxy_app,
        headers={
            "Authorization": "Bearer client-supplied-junk",
            "x-api-key": "sk-client-pretend",
        },
    )

    assert route.called
    upstream_headers = {k.lower(): v for k, v in route.calls[0].request.headers.items()}
    assert upstream_headers["x-api-key"] == "sk-test-server-key"
    assert "authorization" not in upstream_headers


@respx.mock
def test_upstream_sees_anthropic_beta_passthrough(proxy_app: Any) -> None:
    route = respx.post(_UPSTREAM_URL).mock(return_value=httpx.Response(200, json=_OK_RESPONSE))

    _post(proxy_app, headers={"anthropic-beta": "prompt-caching-2024-07-31"})

    upstream_headers = {k.lower(): v for k, v in route.calls[0].request.headers.items()}
    assert upstream_headers["anthropic-beta"] == "prompt-caching-2024-07-31"


@respx.mock
def test_upstream_url_includes_v1_messages_against_configured_base(proxy_app: Any) -> None:
    """Sanity: settings.anthropic_base_url is used as the upstream base."""
    route = respx.post(_UPSTREAM_URL).mock(return_value=httpx.Response(200, json=_OK_RESPONSE))

    _post(proxy_app)

    assert route.called
    assert str(route.calls[0].request.url) == _UPSTREAM_URL


# --- non-2xx forwarding ----------------------------------------------------


@respx.mock
def test_upstream_4xx_is_forwarded_and_no_event_recorded(proxy_app: Any) -> None:
    """An upstream client error reaches the caller; we don't fabricate usage."""
    respx.post(_UPSTREAM_URL).mock(
        return_value=httpx.Response(
            429,
            json={"type": "error", "error": {"type": "rate_limit_error", "message": "slow down"}},
        )
    )

    response = _post(proxy_app)
    assert response.status_code == 429
    assert response.json()["error"]["type"] == "rate_limit_error"

    with get_session() as session:
        events = session.scalars(select(UsageEvent)).all()
    assert events == []


@respx.mock
def test_upstream_5xx_is_forwarded_and_no_event_recorded(proxy_app: Any) -> None:
    respx.post(_UPSTREAM_URL).mock(return_value=httpx.Response(500, json={"type": "error"}))

    response = _post(proxy_app)
    assert response.status_code == 500

    with get_session() as session:
        events = session.scalars(select(UsageEvent)).all()
    assert events == []


# --- streaming -------------------------------------------------------------


_STREAM_OK_BYTES: bytes = (_FIXTURE_DIR / "anthropic_messages_stream_ok.sse").read_bytes()
_STREAM_ERR_BYTES: bytes = (_FIXTURE_DIR / "anthropic_messages_stream_error_mid.sse").read_bytes()
_STREAM_PARTIAL_BYTES: bytes = (
    _FIXTURE_DIR / "anthropic_messages_stream_only_message_start.sse"
).read_bytes()


class _StreamThenRaise(httpx.AsyncByteStream):
    """Yields chunks, then raises — simulates an upstream mid-flight failure.

    Implements httpx's `AsyncByteStream` protocol so it can be passed
    as the `stream=` argument to `httpx.Response`. respx hands the
    response to our proxy as-is, and our `aiter_raw()` loop sees
    chunks for a bit and then the exception.
    """

    def __init__(self, chunks: list[bytes], exc: BaseException) -> None:
        self._chunks = chunks
        self._exc = exc

    async def __aiter__(self) -> Any:
        for c in self._chunks:
            yield c
        raise self._exc

    async def aclose(self) -> None:
        return None


def _stream_post(app: Any, body: dict[str, Any]) -> httpx.Response:
    """Drive the ASGI app with a streaming body request.

    Returns the *fully-buffered* response so tests can introspect
    bytes + status easily. The proxy itself still streams chunks
    end-to-end; the buffering is the test harness's call.
    """

    async def call() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://proxy") as client:
            return await client.post("/v1/messages", json=body, headers={})

    return asyncio.run(call())


@respx.mock
def test_stream_forwards_bytes_unchanged_and_records_success(proxy_app: Any) -> None:
    """Happy path: bytes flow through verbatim, one success row written."""
    respx.post(_UPSTREAM_URL).mock(
        return_value=httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_STREAM_OK_BYTES,
        )
    )

    response = _stream_post(
        proxy_app,
        body={"model": "claude-sonnet-4-6", "messages": [], "stream": True},
    )

    assert response.status_code == 200
    assert response.content == _STREAM_OK_BYTES  # byte-for-byte tee

    with get_session() as session:
        events = session.scalars(select(UsageEvent)).all()
    assert len(events) == 1
    event = events[0]
    assert event.provider == "anthropic"
    assert event.model == "claude-sonnet-4-6"
    assert event.input_tokens == 100
    # output_tokens comes from message_delta (50), NOT message_start's initial 1.
    assert event.output_tokens == 50
    assert event.cache_write_tokens == 10
    assert event.cache_read_tokens == 5
    assert event.request_id == "msg_streamtest_ok_123"
    assert event.success is True
    assert event.error_type is None
    # Same pricing as the non-streaming test (3, 15, 3.75, 0.30) →
    # 100*3 + 50*15 + 10*3.75 + 5*0.30 = 1089.0 USD-per-M-tokens →
    # nano = round(1089 * 1000) = 1_089_000.
    assert event.cost_nano_usd == 1_089_000


@respx.mock
def test_stream_idempotency_via_msg_id_from_message_start(proxy_app: Any) -> None:
    """Two streams returning the same `msg_…` collapse to one row."""
    respx.post(_UPSTREAM_URL).mock(
        return_value=httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_STREAM_OK_BYTES,
        )
    )

    _stream_post(proxy_app, body={"model": "claude-sonnet-4-6", "messages": [], "stream": True})
    _stream_post(proxy_app, body={"model": "claude-sonnet-4-6", "messages": [], "stream": True})

    with get_session() as session:
        events = session.scalars(select(UsageEvent)).all()
    assert len(events) == 1


@respx.mock
def test_stream_mid_stream_error_event_records_failure_with_null_request_id(
    proxy_app: Any,
) -> None:
    """`event: error` after `message_start` -> success=False, request_id=NULL."""
    respx.post(_UPSTREAM_URL).mock(
        return_value=httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_STREAM_ERR_BYTES,
        )
    )

    response = _stream_post(
        proxy_app,
        body={"model": "claude-sonnet-4-6", "messages": [], "stream": True},
    )
    assert response.status_code == 200
    assert response.content == _STREAM_ERR_BYTES  # error event forwarded too

    with get_session() as session:
        events = session.scalars(select(UsageEvent)).all()
    assert len(events) == 1
    event = events[0]
    assert event.success is False
    assert event.error_type == "stream_interrupted"
    assert event.request_id is None  # NULLed per the failure-row contract
    assert event.input_tokens == 80  # captured from message_start
    assert event.output_tokens == 0  # no message_delta ever observed


@respx.mock
def test_stream_upstream_connection_drop_records_connection_dropped(proxy_app: Any) -> None:
    """Upstream TCP drops mid-stream -> `connection_dropped` failure row."""
    respx.post(_UPSTREAM_URL).mock(
        return_value=httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=_StreamThenRaise(
                chunks=[_STREAM_PARTIAL_BYTES],
                exc=httpx.RemoteProtocolError("simulated tcp drop"),
            ),
        )
    )

    # The client may also see the error propagated; we only care that
    # the *capture* path wrote the right row.
    with contextlib.suppress(httpx.RemoteProtocolError):
        _stream_post(
            proxy_app,
            body={"model": "claude-sonnet-4-6", "messages": [], "stream": True},
        )  # client-side observation of the same drop

    with get_session() as session:
        events = session.scalars(select(UsageEvent)).all()
    assert len(events) == 1
    event = events[0]
    assert event.success is False
    assert event.error_type == "connection_dropped"
    assert event.request_id is None
    assert event.input_tokens == 42  # from the partial-fixture's message_start


@respx.mock
def test_stream_aborted_before_message_start_records_nothing(proxy_app: Any) -> None:
    """Asymmetric-rule check: no `message_start` observed -> skip the row."""
    respx.post(_UPSTREAM_URL).mock(
        return_value=httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=_StreamThenRaise(
                chunks=[b""],
                exc=httpx.RemoteProtocolError("dropped before any data"),
            ),
        )
    )

    with contextlib.suppress(httpx.RemoteProtocolError):
        _stream_post(
            proxy_app,
            body={"model": "claude-sonnet-4-6", "messages": [], "stream": True},
        )

    with get_session() as session:
        events = session.scalars(select(UsageEvent)).all()
    assert events == []  # we have no model + no tokens — record nothing


@respx.mock
def test_stream_upstream_4xx_buffered_and_forwarded_no_row(proxy_app: Any) -> None:
    """Non-2xx on a streaming request: forwarded as a normal Response, no row."""
    respx.post(_UPSTREAM_URL).mock(
        return_value=httpx.Response(
            429,
            json={"type": "error", "error": {"type": "rate_limit_error", "message": "slow"}},
        )
    )

    response = _stream_post(
        proxy_app,
        body={"model": "claude-sonnet-4-6", "messages": [], "stream": True},
    )
    assert response.status_code == 429
    assert response.json()["error"]["type"] == "rate_limit_error"

    with get_session() as session:
        events = session.scalars(select(UsageEvent)).all()
    assert events == []


@respx.mock
def test_stream_header_rewrite_still_applies(proxy_app: Any) -> None:
    """Server-side `x-api-key` + default `anthropic-version` apply to streams too."""
    route = respx.post(_UPSTREAM_URL).mock(
        return_value=httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_STREAM_OK_BYTES,
        )
    )

    _stream_post(
        proxy_app,
        body={"model": "claude-sonnet-4-6", "messages": [], "stream": True},
    )

    assert route.called
    upstream_headers = {k.lower(): v for k, v in route.calls[0].request.headers.items()}
    assert upstream_headers["x-api-key"] == "sk-test-server-key"
    assert upstream_headers["anthropic-version"] == "2023-06-01"


# --- best-effort recording -------------------------------------------------


@respx.mock
def test_record_failure_does_not_break_upstream_response(proxy_app: Any, tmp_path: Path) -> None:
    """If recording raises, the user still gets a clean upstream response."""
    # Response is missing `id` (one of the required fields); the recorder
    # logs and skips. The forwarded response is still 200 / unchanged.
    bad_payload = {**_OK_RESPONSE}
    del bad_payload["id"]
    respx.post(_UPSTREAM_URL).mock(return_value=httpx.Response(200, json=bad_payload))

    response = _post(proxy_app)
    assert response.status_code == 200
    assert response.json() == bad_payload

    with get_session() as session:
        events = session.scalars(select(UsageEvent)).all()
    assert events == []


@respx.mock
def test_record_failure_on_db_lock_does_not_break_upstream_response(
    proxy_app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Any exception from record_event must be swallowed (best-effort capture)."""
    from llm_usage.capture import anthropic as anthropic_module

    def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("simulated DB lock")

    monkeypatch.setattr(anthropic_module, "record_event", boom)
    respx.post(_UPSTREAM_URL).mock(return_value=httpx.Response(200, json=_OK_RESPONSE))

    response = _post(proxy_app)
    assert response.status_code == 200
    assert response.json() == _OK_RESPONSE


# --- missing-key 503 -------------------------------------------------------


@respx.mock
def test_missing_anthropic_key_returns_503_without_calling_upstream(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No ANTHROPIC_API_KEY in the proxy env -> 503 configuration_error, no upstream POST.

    Mirrors the per-request 503 path on the OpenAI-compatible routes
    (see test_capture_openai_compat_routes.py). Closes a previously
    asymmetric gap where the Anthropic route would hit `assert key is
    not None` in `_anthropic_common.build_upstream_headers` and
    surface as a 500.
    """
    monkeypatch.setenv("LLM_USAGE_DB_URL", f"sqlite:///{tmp_path / 'usage.db'}")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        monkeypatch.delenv(var, raising=False)
        monkeypatch.delenv(var.lower(), raising=False)
    migrate_to_head()
    route = respx.post(_UPSTREAM_URL).mock(return_value=httpx.Response(200, json=_OK_RESPONSE))
    app = create_proxy_app(Settings())
    app.state.http_client = httpx.AsyncClient(timeout=5.0)
    try:
        response = _post(app)
        assert response.status_code == 503
        body = response.json()
        # Anthropic-shaped envelope: top-level `type: error`, nested `error.type`.
        assert body["type"] == "error"
        assert body["error"]["type"] == "configuration_error"
        assert "ANTHROPIC_API_KEY" in body["error"]["message"]
        assert not route.called
    finally:
        asyncio.run(app.state.http_client.aclose())


@respx.mock
def test_stream_missing_anthropic_key_returns_503_without_calling_upstream(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The streaming dispatch is gated by the same missing-key check.

    The 503 fires in `_handle_messages` *before* the `stream: true`
    branch dispatches to `handle_streaming`, so a streaming request
    with no key never reaches the streaming handler at all.
    """
    monkeypatch.setenv("LLM_USAGE_DB_URL", f"sqlite:///{tmp_path / 'usage.db'}")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        monkeypatch.delenv(var, raising=False)
        monkeypatch.delenv(var.lower(), raising=False)
    migrate_to_head()
    route = respx.post(_UPSTREAM_URL).mock(return_value=httpx.Response(200, json=_OK_RESPONSE))
    app = create_proxy_app(Settings())
    app.state.http_client = httpx.AsyncClient(timeout=5.0)
    try:
        response = _post(
            app,
            body={"model": "claude-sonnet-4-6", "messages": [], "stream": True},
        )
        assert response.status_code == 503
        assert response.json()["error"]["type"] == "configuration_error"
        assert not route.called
    finally:
        asyncio.run(app.state.http_client.aclose())


# --- run_proxy boot sequence ----------------------------------------------


def test_run_proxy_binds_loopback_and_calls_require_keys(
    settings_with_db: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`run_proxy` must call uvicorn with `host=127.0.0.1` and demand the key.

    Mocks `uvicorn.run` (which would otherwise block the test process)
    and `bootstrap` (which is exercised elsewhere). The point is to
    pin the **loopback-only** contract — a regression that changes
    `host` to `0.0.0.0` would silently expose the proxy to the local
    network, and this test catches that.
    """
    import uvicorn

    from llm_usage.capture import proxy as proxy_module
    from llm_usage.config import get_settings

    captured_kwargs: dict[str, object] = {}

    def fake_run(*args: object, **kwargs: object) -> None:
        captured_kwargs.update(kwargs)

    monkeypatch.setattr(uvicorn, "run", fake_run)
    get_settings.cache_clear()  # pick up the fixture's env vars

    proxy_module.run_proxy(port=12345, log_level="WARNING")

    assert captured_kwargs["host"] == "127.0.0.1"
    assert captured_kwargs["port"] == 12345
    assert captured_kwargs["factory"] is True
    assert captured_kwargs["log_level"] == "warning"


def test_run_proxy_warns_but_starts_with_missing_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """No keys configured -> proxy still starts; one WARNING per enabled provider.

    Per-request 503s handle the runtime case (a request to a route
    whose key is missing); the startup-time job is to log warnings so
    a misconfigured user sees the problem before the request fails.
    """
    monkeypatch.setenv("LLM_USAGE_DB_URL", f"sqlite:///{tmp_path / 'usage.db'}")
    for env in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY", "DASHSCOPE_API_KEY"):
        monkeypatch.delenv(env, raising=False)

    import uvicorn

    from llm_usage.capture import proxy as proxy_module
    from llm_usage.config import get_settings

    captured: dict[str, object] = {}

    def fake_run(*args: object, **kwargs: object) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(uvicorn, "run", fake_run)
    get_settings.cache_clear()

    # caplog captures at root by default; widen to WARNING in case the
    # log level inherits something more permissive from a prior test.
    caplog.set_level("WARNING")
    proxy_module.run_proxy()

    # uvicorn was reached — startup didn't bail.
    assert captured.get("host") == "127.0.0.1"
    # Every enabled provider got a warning naming itself.
    warning_records = [r for r in caplog.records if r.levelname == "WARNING"]
    warning_text = "\n".join(r.getMessage() for r in warning_records)
    for provider in ("anthropic", "openai", "deepseek", "qwen"):
        assert provider in warning_text, f"{provider} missing from: {warning_text!r}"
