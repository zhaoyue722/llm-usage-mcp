"""Pytest fixtures shared across the test suite."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

import llm_usage.core.db.session as session_mod
from llm_usage.config import get_settings


@pytest.fixture(autouse=True)
def _isolate_settings(
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    """Isolate every test from the repo `.env`, the cached `Settings`, and
    the cached engine/session-factory in `core.db.session`.

    Three concerns:

    1. `Settings` reads env at construction; tests that `monkeypatch.setenv(...)`
       expect those changes to take effect immediately. Without clearing
       `get_settings`' LRU cache, a stale singleton from an earlier test
       would leak its env snapshot.
    2. pydantic-settings looks for a `.env` file in cwd. If a developer has
       a local `.env`, it would silently override env-only tests. Running
       each test from a fresh empty tmp dir removes that source of flakiness.
       Individual tests that want `.env` loading create one in their own
       `tmp_path` and use `monkeypatch.chdir(tmp_path)` directly.
    3. `core.db.session` caches `_engine` / `_session_factory` at module
       level so production code doesn't rebuild the pool on every call.
       Tests that swap `LLM_USAGE_DB_URL` mid-suite would otherwise see
       the previous test's engine pointing at the previous test's DB.
       Disposing + nulling them before and after each test forces a fresh
       build keyed off the current env.
    """
    isolated_cwd = tmp_path_factory.mktemp("no_dotenv")
    monkeypatch.chdir(isolated_cwd)
    get_settings.cache_clear()
    _reset_session_singletons()
    try:
        yield
    finally:
        _reset_session_singletons()
        get_settings.cache_clear()


def _reset_session_singletons() -> None:
    if session_mod._engine is not None:
        session_mod._engine.dispose()
    session_mod._engine = None
    session_mod._session_factory = None
