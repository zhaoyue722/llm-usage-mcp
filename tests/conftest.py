"""Pytest fixtures shared across the test suite."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from llm_usage.config import get_settings


@pytest.fixture(autouse=True)
def _isolate_settings(
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    """Isolate every test from the repo `.env` and the cached `Settings`.

    Two concerns:

    1. `Settings` reads env at construction; tests that `monkeypatch.setenv(...)`
       expect those changes to take effect immediately. Without clearing
       `get_settings`' LRU cache, a stale singleton from an earlier test
       would leak its env snapshot.
    2. pydantic-settings looks for a `.env` file in cwd. If a developer has
       a local `.env`, it would silently override env-only tests. Running
       each test from a fresh empty tmp dir removes that source of flakiness.
       Individual tests that want `.env` loading create one in their own
       `tmp_path` and use `monkeypatch.chdir(tmp_path)` directly.
    """
    isolated_cwd = tmp_path_factory.mktemp("no_dotenv")
    monkeypatch.chdir(isolated_cwd)
    get_settings.cache_clear()
    try:
        yield
    finally:
        get_settings.cache_clear()
