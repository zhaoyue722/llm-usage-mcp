"""Integration tests for the OpenAI-compatible routes.

Drives the FastAPI app via `httpx.ASGITransport` (no real network bind,
no real port) and mocks each upstream with respx — symmetric with
`test_capture_proxy.py`'s Anthropic coverage. One test file rather
than three: the three providers share enough behavior that a per-
provider parametrize is the right shape; the differences are pinned
in the unit tests (`test_capture_openai_compatible.py`).
"""

from __future__ import annotations

import asyncio
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

# Shape: (provider, prefix, upstream URL, env var, fixture filename,
#         expected msg id, expected input/output, expected cache (read, write),
#         expected cost in nano-USD given the seeded pricing).
#
# The pricing seeded below uses simple per-million rates (input=$1/M,
# output=$2/M, cache_read=$0.5/M, cache_write=$0/M) so per-row costs
# can be asserted exactly from the fixture token counts.
_CASES = [
    {
        "id": "openai",
        "provider": "openai",
        "prefix": "/openai",
        "upstream": "https://api.openai.com/v1/chat/completions",
        "env_var": "OPENAI_API_KEY",
        "fixture": "openai_chat_completions_ok.json",
        "msg_id": "chatcmpl-openaitest123",
        # uncached input = 20 - 8 = 12, cache_read = 8, output = 30
        "input_tokens": 12,
        "cache_read_tokens": 8,
        "output_tokens": 30,
        # cost = 12 * $1/M + 30 * $2/M + 8 * $0.5/M = $76 / 1M = 76_000 nano
        "cost_nano": 76_000,
    },
    {
        "id": "deepseek",
        "provider": "deepseek",
        "prefix": "/deepseek",
        "upstream": "https://api.deepseek.com/chat/completions",
        "env_var": "DEEPSEEK_API_KEY",
        "fixture": "deepseek_chat_completions_ok.json",
        "msg_id": "chatcmpl-deepseektest456",
        # miss = 10, hit = 4, output = 25
        "input_tokens": 10,
        "cache_read_tokens": 4,
        "output_tokens": 25,
        # cost = 10 * $1/M + 25 * $2/M + 4 * $0.5/M = $62 / 1M = 62_000 nano
        "cost_nano": 62_000,
    },
    {
        "id": "qwen",
        "provider": "qwen",
        "prefix": "/qwen",
        "upstream": "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
        "env_var": "DASHSCOPE_API_KEY",
        "fixture": "qwen_chat_completions_ok.json",
        "msg_id": "chatcmpl-qwentest789",
        # input = 18, no cache fields, output = 12
        "input_tokens": 18,
        "cache_read_tokens": 0,
        "output_tokens": 12,
        # cost = 18 * $1/M + 12 * $2/M + 0 = $42 / 1M = 42_000 nano
        "cost_nano": 42_000,
    },
]


def _model_for(provider: str) -> str:
    """Pick a representative model name for fixture seeding."""
    return {"openai": "gpt-5.2", "deepseek": "deepseek-chat", "qwen": "qwen-turbo"}[provider]


@pytest.fixture
def settings_with_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    """Fresh DB seeded with pricing for all three providers + server-side keys."""
    monkeypatch.setenv("LLM_USAGE_DB_URL", f"sqlite:///{tmp_path / 'usage.db'}")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-openai-server")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-deepseek-server")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-test-dashscope-server")
    # Strip ambient proxy env vars — see the comment in test_capture_proxy.py.
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


def _post(
    app: Any,
    path: str,
    body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    """Drive the ASGI app — synchronous wrapper, same pattern as the Anthropic tests."""

    async def call() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://proxy") as client:
            return await client.post(
                path,
                json=body or {"model": "x", "messages": []},
                headers=headers or {},
            )

    return asyncio.run(call())


# --- happy path per provider -----------------------------------------------


@pytest.mark.parametrize("case", _CASES, ids=[str(c["id"]) for c in _CASES])
@respx.mock
def test_forwards_response_unchanged_and_records_usage(
    case: dict[str, Any], proxy_app: Any
) -> None:
    """2xx upstream -> client gets the body, event lands with per-provider tokens."""
    payload = json.loads((_FIXTURE_DIR / case["fixture"]).read_text())
    respx.post(case["upstream"]).mock(return_value=httpx.Response(200, json=payload))

    response = _post(
        proxy_app,
        f"{case['prefix']}/v1/chat/completions",
        body={"model": _model_for(case["provider"]), "messages": []},
    )

    assert response.status_code == 200
    assert response.json() == payload  # forwarded verbatim

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


# --- header rewrite (per provider) -----------------------------------------


@pytest.mark.parametrize("case", _CASES, ids=[str(c["id"]) for c in _CASES])
@respx.mock
def test_upstream_sees_server_side_bearer_not_client_auth(
    case: dict[str, Any], proxy_app: Any
) -> None:
    """Client's Authorization MUST NOT survive the rewrite — server-side key only."""
    payload = json.loads((_FIXTURE_DIR / case["fixture"]).read_text())
    route = respx.post(case["upstream"]).mock(return_value=httpx.Response(200, json=payload))

    _post(
        proxy_app,
        f"{case['prefix']}/v1/chat/completions",
        body={"model": _model_for(case["provider"]), "messages": []},
        headers={"Authorization": "Bearer client-supplied-junk", "x-api-key": "sk-junk"},
    )

    assert route.called
    upstream_headers = {k.lower(): v for k, v in route.calls[0].request.headers.items()}
    expected_key = {
        "openai": "sk-test-openai-server",
        "deepseek": "sk-test-deepseek-server",
        "qwen": "sk-test-dashscope-server",
    }[case["provider"]]
    assert upstream_headers["authorization"] == f"Bearer {expected_key}"


# --- non-2xx and edge cases ------------------------------------------------


@respx.mock
def test_upstream_4xx_is_forwarded_and_no_event_recorded(proxy_app: Any) -> None:
    """An upstream client error reaches the caller; we don't fabricate usage."""
    respx.post("https://api.openai.com/v1/chat/completions").mock(
        return_value=httpx.Response(
            429,
            json={"error": {"message": "slow down", "type": "rate_limit_error"}},
        )
    )

    response = _post(
        proxy_app,
        "/openai/v1/chat/completions",
        body={"model": "gpt-5.2", "messages": []},
    )
    assert response.status_code == 429
    assert response.json()["error"]["type"] == "rate_limit_error"

    with get_session() as session:
        events = session.scalars(select(UsageEvent)).all()
    assert events == []


# `stream: true` is no longer rejected — it dispatches to the
# streaming handler. That behavior is covered end-to-end in
# `test_capture_openai_compat_streaming_routes.py`.


def test_missing_key_returns_503_without_calling_upstream(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No DEEPSEEK_API_KEY set -> 503 configuration_error; the route still exists."""
    monkeypatch.setenv("LLM_USAGE_DB_URL", f"sqlite:///{tmp_path / 'usage.db'}")
    for env in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY", "DASHSCOPE_API_KEY"):
        monkeypatch.delenv(env, raising=False)
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        monkeypatch.delenv(var, raising=False)
        monkeypatch.delenv(var.lower(), raising=False)
    migrate_to_head()
    settings = Settings()
    app = create_proxy_app(settings)
    app.state.http_client = httpx.AsyncClient(timeout=5.0)
    try:
        response = _post(
            app,
            "/deepseek/v1/chat/completions",
            body={"model": "deepseek-chat", "messages": []},
        )
        assert response.status_code == 503
        body = response.json()
        assert body["error"]["type"] == "configuration_error"
        assert "DEEPSEEK_API_KEY" in body["error"]["message"]
    finally:
        asyncio.run(app.state.http_client.aclose())


@respx.mock
def test_idempotency_via_response_id(proxy_app: Any) -> None:
    """Two identical responses (same `id`) yield exactly one row.

    Symmetric with the Anthropic msg_id dedup — the OpenAI-family
    `id` (`chatcmpl-...`) becomes our `request_id`, and the UNIQUE
    index collapses replays.
    """
    payload = json.loads((_FIXTURE_DIR / "openai_chat_completions_ok.json").read_text())
    respx.post("https://api.openai.com/v1/chat/completions").mock(
        return_value=httpx.Response(200, json=payload)
    )

    _post(proxy_app, "/openai/v1/chat/completions", body={"model": "gpt-5.2", "messages": []})
    _post(proxy_app, "/openai/v1/chat/completions", body={"model": "gpt-5.2", "messages": []})

    with get_session() as session:
        events = session.scalars(select(UsageEvent)).all()
    assert len(events) == 1


@respx.mock
def test_record_failure_does_not_break_upstream_response(proxy_app: Any) -> None:
    """If recording's shape check rejects, the user still gets the upstream 200.

    Same posture as the Anthropic handler — capture is best-effort.
    """
    bad_payload = {
        "id": "chatcmpl-x",
        "model": "gpt-5.2",
        "usage": "not-a-dict-which-fails-the-shape-check",
    }
    respx.post("https://api.openai.com/v1/chat/completions").mock(
        return_value=httpx.Response(200, json=bad_payload)
    )

    response = _post(
        proxy_app,
        "/openai/v1/chat/completions",
        body={"model": "gpt-5.2", "messages": []},
    )
    assert response.status_code == 200
    assert response.json() == bad_payload

    with get_session() as session:
        events = session.scalars(select(UsageEvent)).all()
    assert events == []
