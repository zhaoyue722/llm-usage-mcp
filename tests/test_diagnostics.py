"""Unit tests for `core.diagnostics.collect_status`.

The diagnostic collector is pure read-side — it should never create
files, never call out to the network without `check_proxy=True`, and
return a fully-typed `StatusReport`. These tests pin those guarantees
against tmp-path DBs with the various states a real install can be
in: missing DB, freshly-migrated DB, populated DB, schema behind head.

`tests/conftest.py` already isolates `Settings` and the cached
session factory, so flipping `LLM_USAGE_DB_URL` via `monkeypatch.setenv`
is enough to point each test at its own DB.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from llm_usage.bootstrap import migrate_to_head
from llm_usage.config import Settings, get_settings
from llm_usage.core.db.session import get_session
from llm_usage.core.diagnostics import collect_status
from llm_usage.core.pricing import Pricing, upsert_pricing
from llm_usage.core.recording import record_event


@pytest.fixture
def settings_with_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Settings, Path]:
    """Settings pointing at a tmp DB path that *doesn't yet exist*.

    The point of the diagnostic is precisely to handle "DB hasn't
    been initialized yet" gracefully, so the path is deliberately
    not pre-created. Tests that want a migrated DB call
    `migrate_to_head()` explicitly inside the test body.
    """
    db = tmp_path / "usage.db"
    monkeypatch.setenv("LLM_USAGE_DB_URL", f"sqlite:///{db}")
    return get_settings(), db


# --- "DB doesn't exist" path ---------------------------------------------


def test_collect_status_returns_none_for_database_when_file_missing(
    settings_with_db: tuple[Settings, Path],
) -> None:
    settings, db = settings_with_db
    assert not db.exists()  # precondition

    report = collect_status(settings, check_proxy=False)
    assert report.database is None
    assert report.pricing is None
    # Provider list still populated — keys / URLs are settings, not DB-backed.
    assert len(report.providers) == 4


def test_collect_status_does_not_create_files_when_db_missing(
    settings_with_db: tuple[Settings, Path],
) -> None:
    """`status` is observational; running it must not bootstrap the DB."""
    settings, db = settings_with_db
    collect_status(settings, check_proxy=False)
    assert not db.exists()


def test_collect_status_providers_report_zero_models_when_db_missing(
    settings_with_db: tuple[Settings, Path],
) -> None:
    settings, _ = settings_with_db
    report = collect_status(settings, check_proxy=False)
    for provider in report.providers:
        assert provider.model_count == 0


# --- migrated-but-empty DB -----------------------------------------------


def test_collect_status_after_migration_reports_empty_state(
    settings_with_db: tuple[Settings, Path],
) -> None:
    settings, db = settings_with_db
    migrate_to_head()

    report = collect_status(settings, check_proxy=False)
    assert report.database is not None
    assert report.database.path == str(db)
    assert report.database.event_count == 0
    assert report.database.oldest_event_ms is None
    assert report.database.newest_event_ms is None
    assert report.database.schema_at_head is True
    assert report.database.schema_revision is not None
    # Pricing exists (the migration creates the table) but it's empty
    # until something seeds it.
    assert report.pricing is not None
    assert report.pricing.model_count == 0
    assert report.pricing.provider_count == 0


# --- populated DB ---------------------------------------------------------


def test_collect_status_populated_db_reports_event_range_and_pricing_count(
    settings_with_db: tuple[Settings, Path],
) -> None:
    settings, _ = settings_with_db
    migrate_to_head()

    now_ms = int(time.time() * 1000)
    pricings = [
        Pricing(
            provider="anthropic",
            model="m1",
            input_per_million_usd=3.0,
            output_per_million_usd=15.0,
            fetched_at=now_ms,
        ),
        Pricing(
            provider="anthropic",
            model="m2",
            input_per_million_usd=1.0,
            output_per_million_usd=2.0,
            fetched_at=now_ms,
        ),
        Pricing(
            provider="openai",
            model="m3",
            input_per_million_usd=2.0,
            output_per_million_usd=4.0,
            fetched_at=now_ms,
        ),
    ]
    with get_session() as session:
        upsert_pricing(session, pricings)
        # Two events with controlled timestamps. record_event stamps
        # them with `time.time()`; for an ordering assertion we just
        # need both to land between the start and end of this test.
        record_event(
            session,
            provider="anthropic",
            model="m1",
            input_tokens=100,
            output_tokens=10,
            success=True,
            request_id="a",
        )
        record_event(
            session,
            provider="openai",
            model="m3",
            input_tokens=200,
            output_tokens=20,
            success=True,
            request_id="b",
        )
        session.commit()

    report = collect_status(settings, check_proxy=False)
    assert report.database is not None
    assert report.database.event_count == 2
    assert report.database.oldest_event_ms is not None
    assert report.database.newest_event_ms is not None
    assert report.database.oldest_event_ms <= report.database.newest_event_ms

    assert report.pricing is not None
    assert report.pricing.model_count == 3
    assert report.pricing.provider_count == 2

    # Per-provider model counts roll up correctly.
    by_name = {p.name: p for p in report.providers}
    assert by_name["anthropic"].model_count == 2
    assert by_name["openai"].model_count == 1
    assert by_name["deepseek"].model_count == 0
    assert by_name["qwen"].model_count == 0


# --- proxy probe ---------------------------------------------------------


def test_collect_status_check_proxy_false_reports_reachable_none(
    settings_with_db: tuple[Settings, Path],
) -> None:
    """`--no-net` plumbs through to `reachable=None` so the renderer
    can show `unknown` instead of a misleading `not running`."""
    settings, _ = settings_with_db
    report = collect_status(settings, check_proxy=False)
    assert report.proxy.reachable is None
    assert report.proxy.host == "127.0.0.1"
    assert report.proxy.port == settings.proxy_port


def test_collect_status_check_proxy_true_against_idle_port_reports_unreachable(
    settings_with_db: tuple[Settings, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An idle port should resolve to `reachable=False` within the
    short probe timeout. We override the proxy port to one well
    outside the dynamic range to minimize the chance of a real
    listener squatting on it in the test environment."""
    monkeypatch.setenv("LLM_USAGE_PROXY_PORT", "1")  # port 1 is privileged, no listener
    get_settings.cache_clear()
    settings = get_settings()
    report = collect_status(settings, check_proxy=True)
    assert report.proxy.reachable is False


# --- provider key detection ----------------------------------------------


def test_collect_status_provider_key_set_reflects_env(
    settings_with_db: tuple[Settings, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One key set, three unset — checks the cross-product against
    `KNOWN_PROVIDERS` lights up only the right row."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    get_settings.cache_clear()
    settings = get_settings()

    report = collect_status(settings, check_proxy=False)
    by_name = {p.name: p for p in report.providers}
    assert by_name["anthropic"].key_set is True
    assert by_name["openai"].key_set is False
    assert by_name["deepseek"].key_set is False
    assert by_name["qwen"].key_set is False


def test_collect_status_providers_carry_branded_display_names(
    settings_with_db: tuple[Settings, Path],
) -> None:
    """`StatusProvider.display_name` is the rendered string —
    "OpenAI" / "DeepSeek" CamelCase, not `.title()` collapse."""
    settings, _ = settings_with_db
    report = collect_status(settings, check_proxy=False)
    by_name = {p.name: p for p in report.providers}
    assert by_name["anthropic"].display_name == "Anthropic"
    assert by_name["openai"].display_name == "OpenAI"
    assert by_name["deepseek"].display_name == "DeepSeek"
    assert by_name["qwen"].display_name == "Qwen"


def test_collect_status_providers_carry_base_url_overrides(
    settings_with_db: tuple[Settings, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A `LLM_USAGE_ANTHROPIC_BASE_URL` override should show up in
    `StatusProvider.base_url` — important for users who route
    through a third-party reverse proxy."""
    monkeypatch.setenv("LLM_USAGE_ANTHROPIC_BASE_URL", "https://proxy.example.com")
    get_settings.cache_clear()
    settings = get_settings()
    report = collect_status(settings, check_proxy=False)
    by_name = {p.name: p for p in report.providers}
    assert by_name["anthropic"].base_url == "https://proxy.example.com"


# --- version --------------------------------------------------------------


def test_collect_status_reports_a_version_string(
    settings_with_db: tuple[Settings, Path],
) -> None:
    """In an editable install the version comes from package metadata.
    Either it resolves to a real version, or the helper falls back to
    `"unknown"` — both are valid; the field must just be non-empty."""
    settings, _ = settings_with_db
    report = collect_status(settings, check_proxy=False)
    assert report.version
    assert isinstance(report.version, str)
