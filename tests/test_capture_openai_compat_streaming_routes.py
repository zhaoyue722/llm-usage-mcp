"""Integration tests for the OpenAI-compatible streaming routes.

Drives the FastAPI app via `httpx.ASGITransport`, mocks each upstream
with respx. The test client's `.post()` reads the streamed response to
completion, which drives `_iter_with_recording` to its end and
triggers the usage-record write — so a row (or no row) can be asserted
right after the call returns.
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

# Same per-million rates as the non-streaming routes test: input $1/M,
# output $2/M, cache_read $0.5/M, cache_write $0/M — round numbers so
# per-row cost asserts to an exact nano-USD figure.
_CASES = [
    {
        "id": "openai",
        "provider": "openai",
        "prefix": "/openai",
        "upstream": "https://api.openai.com/v1/chat/completions",
        "model": "gpt-5.2",
        "fixture": "openai_chat_completions_stream_ok.sse",
        "msg_id": "chatcmpl-streamopenai123",
        # prompt_tokens=20, cached=8 → input=12; output=30; cache_read=8
        "input_tokens": 12,
        "output_tokens": 30,
        "cache_read_tokens": 8,
        # 12*1 + 30*2 + 8*0.5 = 76 → 76_000 nano
        "cost_nano": 76_000,
    },
    {
        "id": "deepseek",
        "provider": "deepseek",
        "prefix": "/deepseek",
        "upstream": "https://api.deepseek.com/chat/completions",
        "model": "deepseek-chat",
        "fixture": "deepseek_chat_completions_stream_ok.sse",
        "msg_id": "chatcmpl-streamdeepseek456",
        # miss=10, hit=4, output=25
        "input_tokens": 10,
        "output_tokens": 25,
        "cache_read_tokens": 4,
        # 10*1 + 25*2 + 4*0.5 = 62 → 62_000 nano
        "cost_nano": 62_000,
    },
    {
        "id": "qwen",
        "provider": "qwen",
        "prefix": "/qwen",
        "upstream": "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
        "model": "qwen-turbo",
        "fixture": "qwen_chat_completions_stream_ok.sse",
        "msg_id": "chatcmpl-streamqwen789",
        # input=18, no cache, output=12
        "input_tokens": 18,
        "output_tokens": 12,
        "cache_read_tokens": 0,
        # 18*1 + 12*2 = 42 → 42_000 nano
        "cost_nano": 42_000,
    },
]


class _StreamThenRaise(httpx.AsyncByteStream):
    """Yields chunks, then raises — simulates an upstream mid-stream failure."""

    def __init__(self, chunks: list[bytes], exc: BaseException) -> None:
        self._chunks = chunks
        self._exc = exc

    async def __aiter__(self) -> Any:
        for c in self._chunks:
            yield c
        raise self._exc

    async def aclose(self) -> None:
        return None


@pytest.fixture
def settings_with_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    """Fresh DB seeded with pricing for all three providers + server-side keys."""
    monkeypatch.setenv("LLM_USAGE_DB_URL", f"sqlite:///{tmp_path / 'usage.db'}")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-openai-server")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-deepseek-server")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-test-dashscope-server")
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
                Pricing(
                    "openai",
                    "gpt-5.2",
                    input_per_million_usd=1.0,
                    output_per_million_usd=2.0,
                    cache_write_per_million_usd=0.0,
                    cache_read_per_million_usd=0.5,
                    fetched_at=1,
                ),
                Pricing(
                    "deepseek",
                    "deepseek-chat",
                    input_per_million_usd=1.0,
                    output_per_million_usd=2.0,
                    cache_write_per_million_usd=0.0,
                    cache_read_per_million_usd=0.5,
                    fetched_at=1,
                ),
                Pricing(
                    "qwen",
                    "qwen-turbo",
                    input_per_million_usd=1.0,
                    output_per_million_usd=2.0,
                    fetched_at=1,
                ),
            ],
        )
        session.commit()
    return Settings()


@pytest.fixture
def proxy_app(settings_with_db: Settings) -> Iterator[Any]:
    """Build the FastAPI app and seed `state.http_client` directly."""
    app = create_proxy_app(settings_with_db)
    app.state.http_client = httpx.AsyncClient(timeout=30.0)
    try:
        yield app
    finally:
        asyncio.run(app.state.http_client.aclose())


def _stream_post(app: Any, path: str, body: dict[str, Any]) -> httpx.Response:
    """Drive the ASGI app with a streaming request; return the buffered response."""

    async def call() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://proxy") as client:
            return await client.post(path, json=body, headers={})

    return asyncio.run(call())


# --- happy path per provider -----------------------------------------------


@pytest.mark.parametrize("case", _CASES, ids=[str(c["id"]) for c in _CASES])
@respx.mock
def test_stream_forwards_bytes_and_records_usage(case: dict[str, Any], proxy_app: Any) -> None:
    """SSE bytes flow through verbatim; the terminal usage chunk → one success row."""
    sse_bytes = (_FIXTURE_DIR / case["fixture"]).read_bytes()
    respx.post(case["upstream"]).mock(
        return_value=httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=sse_bytes,
        )
    )

    response = _stream_post(
        proxy_app,
        f"{case['prefix']}/v1/chat/completions",
        body={"model": case["model"], "messages": [], "stream": True},
    )

    assert response.status_code == 200
    assert response.content == sse_bytes  # byte-for-byte tee

    with get_session() as session:
        events = session.scalars(select(UsageEvent)).all()
    assert len(events) == 1
    event = events[0]
    assert event.provider == case["provider"]
    assert event.input_tokens == case["input_tokens"]
    assert event.output_tokens == case["output_tokens"]
    assert event.cache_read_tokens == case["cache_read_tokens"]
    assert event.cache_write_tokens == 0
    assert event.request_id == case["msg_id"]
    assert event.cost_nano_usd == case["cost_nano"]
    assert event.success is True
    assert event.error_type is None


# --- include_usage injection -----------------------------------------------


@respx.mock
def test_stream_injects_include_usage_when_absent(proxy_app: Any) -> None:
    """A request without stream_options gets include_usage=true before forwarding."""
    sse_bytes = (_FIXTURE_DIR / "openai_chat_completions_stream_ok.sse").read_bytes()
    route = respx.post("https://api.openai.com/v1/chat/completions").mock(
        return_value=httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=sse_bytes
        )
    )

    _stream_post(
        proxy_app,
        "/openai/v1/chat/completions",
        body={"model": "gpt-5.2", "messages": [], "stream": True},
    )

    assert route.called
    upstream_body = json.loads(route.calls[0].request.content)
    assert upstream_body["stream_options"] == {"include_usage": True}


@respx.mock
def test_stream_respects_explicit_include_usage_false(proxy_app: Any) -> None:
    """include_usage=false is honored: forwarded as-is, and no row recorded.

    The client opted out, so the upstream sends no usage chunk and the
    capture path has nothing to record.
    """
    no_usage = (_FIXTURE_DIR / "openai_chat_completions_stream_no_usage.sse").read_bytes()
    route = respx.post("https://api.openai.com/v1/chat/completions").mock(
        return_value=httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=no_usage
        )
    )

    response = _stream_post(
        proxy_app,
        "/openai/v1/chat/completions",
        body={
            "model": "gpt-5.2",
            "messages": [],
            "stream": True,
            "stream_options": {"include_usage": False},
        },
    )
    assert response.status_code == 200

    # The proxy did not flip the client's explicit choice.
    upstream_body = json.loads(route.calls[0].request.content)
    assert upstream_body["stream_options"] == {"include_usage": False}

    # No usage chunk arrived → nothing to record.
    with get_session() as session:
        events = session.scalars(select(UsageEvent)).all()
    assert events == []


# --- no usage chunk --------------------------------------------------------


@respx.mock
def test_stream_without_usage_chunk_records_nothing(proxy_app: Any) -> None:
    """A 2xx stream that carries no usage chunk → bytes forwarded, no row."""
    no_usage = (_FIXTURE_DIR / "openai_chat_completions_stream_no_usage.sse").read_bytes()
    respx.post("https://api.openai.com/v1/chat/completions").mock(
        return_value=httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=no_usage
        )
    )

    response = _stream_post(
        proxy_app,
        "/openai/v1/chat/completions",
        body={"model": "gpt-5.2", "messages": [], "stream": True},
    )
    assert response.status_code == 200
    assert response.content == no_usage

    with get_session() as session:
        events = session.scalars(select(UsageEvent)).all()
    assert events == []


# --- non-2xx + mid-stream failure ------------------------------------------


@respx.mock
def test_stream_upstream_4xx_is_buffered_and_forwarded_no_row(proxy_app: Any) -> None:
    """Non-2xx on a streaming request: buffered JSON error forwarded, no row."""
    respx.post("https://api.openai.com/v1/chat/completions").mock(
        return_value=httpx.Response(
            429,
            json={"error": {"message": "slow down", "type": "rate_limit_error"}},
        )
    )

    response = _stream_post(
        proxy_app,
        "/openai/v1/chat/completions",
        body={"model": "gpt-5.2", "messages": [], "stream": True},
    )
    assert response.status_code == 429
    assert response.json()["error"]["type"] == "rate_limit_error"

    with get_session() as session:
        events = session.scalars(select(UsageEvent)).all()
    assert events == []


@respx.mock
def test_stream_connection_drop_before_usage_records_nothing(proxy_app: Any) -> None:
    """Upstream TCP drop mid-stream, before the usage chunk → no row.

    OpenAI-family streaming has no early usage signal (unlike
    Anthropic's `message_start`), so a mid-stream death leaves nothing
    honest to record.
    """
    # A couple of content chunks, then the connection dies.
    partial = (
        b'data: {"id":"chatcmpl-x","model":"gpt-5.2","choices":[{"delta":{"content":"Hel"}}]}\n\n'
    )
    respx.post("https://api.openai.com/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=_StreamThenRaise(
                chunks=[partial],
                exc=httpx.RemoteProtocolError("simulated tcp drop"),
            ),
        )
    )

    with contextlib.suppress(httpx.RemoteProtocolError):
        _stream_post(
            proxy_app,
            "/openai/v1/chat/completions",
            body={"model": "gpt-5.2", "messages": [], "stream": True},
        )

    with get_session() as session:
        events = session.scalars(select(UsageEvent)).all()
    assert events == []


@respx.mock
def test_stream_missing_key_returns_503(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A streaming request to a route with no key → 503, no upstream call."""
    monkeypatch.setenv("LLM_USAGE_DB_URL", f"sqlite:///{tmp_path / 'usage.db'}")
    for env in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY", "DASHSCOPE_API_KEY"):
        monkeypatch.delenv(env, raising=False)
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        monkeypatch.delenv(var, raising=False)
        monkeypatch.delenv(var.lower(), raising=False)
    migrate_to_head()
    route = respx.post("https://api.openai.com/v1/chat/completions").mock(
        return_value=httpx.Response(200, content=b"")
    )
    app = create_proxy_app(Settings())
    app.state.http_client = httpx.AsyncClient(timeout=5.0)
    try:
        response = _stream_post(
            app,
            "/openai/v1/chat/completions",
            body={"model": "gpt-5.2", "messages": [], "stream": True},
        )
        assert response.status_code == 503
        assert response.json()["error"]["type"] == "configuration_error"
        assert not route.called
    finally:
        asyncio.run(app.state.http_client.aclose())
