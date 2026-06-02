"""Unit tests for `core.providers.collect_providers`.

The provider collector is pure read-side — like `collect_status`, it
must never create files and must surface every `KNOWN_PROVIDERS` row
even when pricing hasn't been seeded. These tests pin those guarantees
against tmp-path DBs in the states a real install can be in: missing
DB, freshly-migrated DB, populated DB.

`tests/conftest.py` already isolates `Settings` and the cached session
factory, so flipping `LLM_USAGE_DB_URL` via `monkeypatch.setenv` is
enough to point each test at its own DB.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from llm_usage.bootstrap import migrate_to_head
from llm_usage.config import Settings, get_settings
from llm_usage.core.db.session import get_session
from llm_usage.core.pricing import Pricing, upsert_pricing
from llm_usage.core.providers import OPENAI_COMPATIBLE, collect_providers


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


def test_collect_providers_returns_every_known_provider_when_db_missing(
    settings_with_db: tuple[Settings, Path],
) -> None:
    """Even with no DB file, every `KNOWN_PROVIDERS` entry must surface
    — the user expects to see their full config regardless of whether
    pricing has been seeded."""
    settings, db = settings_with_db
    assert not db.exists()  # precondition

    report = collect_providers(settings)
    names = {p.name for p in report.providers}
    assert names == {"anthropic", "openai", "deepseek", "qwen"}


def test_collect_providers_does_not_create_db_file(
    settings_with_db: tuple[Settings, Path],
) -> None:
    """Observational. Running `collect_providers` must not bootstrap
    SQLite — the same rule that `collect_status` enforces."""
    settings, db = settings_with_db
    collect_providers(settings)
    assert not db.exists()


def test_collect_providers_reports_zero_models_when_db_missing(
    settings_with_db: tuple[Settings, Path],
) -> None:
    settings, _ = settings_with_db
    report = collect_providers(settings)
    for provider in report.providers:
        assert provider.models == []


# --- migrated-but-empty DB -----------------------------------------------


def test_collect_providers_migrated_empty_db_reports_zero_models(
    settings_with_db: tuple[Settings, Path],
) -> None:
    """An initialized-but-unseeded DB should look the same as no DB
    from the model-count angle — the catalog is empty either way."""
    settings, _ = settings_with_db
    migrate_to_head()

    report = collect_providers(settings)
    for provider in report.providers:
        assert provider.models == []


# --- populated DB ---------------------------------------------------------


def test_collect_providers_returns_priced_model_lists_sorted(
    settings_with_db: tuple[Settings, Path],
) -> None:
    """The model list for each provider should be alphabetical."""
    settings, _ = settings_with_db
    migrate_to_head()

    now_ms = int(time.time() * 1000)
    pricings = [
        # Insert deliberately *out* of alphabetical order so we know
        # the sort happens inside `collect_providers`, not just by
        # accident of insertion order.
        Pricing(
            provider="anthropic",
            model="m2",
            input_per_million_usd=3.0,
            output_per_million_usd=15.0,
            fetched_at=now_ms,
        ),
        Pricing(
            provider="anthropic",
            model="m1",
            input_per_million_usd=3.0,
            output_per_million_usd=15.0,
            fetched_at=now_ms,
        ),
        Pricing(
            provider="openai",
            model="gpt-4o",
            input_per_million_usd=2.0,
            output_per_million_usd=4.0,
            fetched_at=now_ms,
        ),
    ]
    with get_session() as session:
        upsert_pricing(session, pricings)
        session.commit()

    report = collect_providers(settings)
    by_name = {p.name: p for p in report.providers}
    assert by_name["anthropic"].models == ["m1", "m2"]
    assert by_name["openai"].models == ["gpt-4o"]
    assert by_name["deepseek"].models == []
    assert by_name["qwen"].models == []


# --- key state -----------------------------------------------------------


def test_collect_providers_reflects_env_key_state(
    settings_with_db: tuple[Settings, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One key set, three unset — the right row lights up."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    get_settings.cache_clear()
    settings = get_settings()

    report = collect_providers(settings)
    by_name = {p.name: p for p in report.providers}
    assert by_name["anthropic"].key_set is True
    assert by_name["openai"].key_set is False
    assert by_name["deepseek"].key_set is False
    assert by_name["qwen"].key_set is False


# --- branded display + openai-compat ------------------------------------


def test_collect_providers_carries_branded_display_names(
    settings_with_db: tuple[Settings, Path],
) -> None:
    """CamelCase brands, not `.title()` collapse — same expectation as
    the `status` Providers block."""
    settings, _ = settings_with_db
    report = collect_providers(settings)
    by_name = {p.name: p for p in report.providers}
    assert by_name["anthropic"].display_name == "Anthropic"
    assert by_name["openai"].display_name == "OpenAI"
    assert by_name["deepseek"].display_name == "DeepSeek"
    assert by_name["qwen"].display_name == "Qwen"


def test_collect_providers_openai_compatible_flag_matches_static_map(
    settings_with_db: tuple[Settings, Path],
) -> None:
    """The `openai_compatible` field on each row must agree with the
    centralized `OPENAI_COMPATIBLE` constant — that constant is the
    single source of truth shared with the MCP `list_providers` tool,
    so a drift here would diverge the two surfaces."""
    settings, _ = settings_with_db
    report = collect_providers(settings)
    by_name = {p.name: p for p in report.providers}
    for name, flag in OPENAI_COMPATIBLE.items():
        assert by_name[name].openai_compatible is flag


# --- base URL ------------------------------------------------------------


def test_collect_providers_carries_base_url_overrides(
    settings_with_db: tuple[Settings, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A `LLM_USAGE_*_BASE_URL` override should show up verbatim."""
    monkeypatch.setenv("LLM_USAGE_ANTHROPIC_BASE_URL", "https://proxy.example.com")
    get_settings.cache_clear()
    settings = get_settings()

    report = collect_providers(settings)
    by_name = {p.name: p for p in report.providers}
    assert by_name["anthropic"].base_url == "https://proxy.example.com"


# --- ordering ------------------------------------------------------------


def test_collect_providers_orders_rows_by_display_name(
    settings_with_db: tuple[Settings, Path],
) -> None:
    """Stable, branded reading order: Anthropic, DeepSeek, OpenAI, Qwen."""
    settings, _ = settings_with_db
    report = collect_providers(settings)
    names = [p.display_name for p in report.providers]
    assert names == sorted(names)
    assert names == ["Anthropic", "DeepSeek", "OpenAI", "Qwen"]
