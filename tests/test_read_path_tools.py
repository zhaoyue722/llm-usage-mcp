"""Tests for the read-path MCP surface against a real seeded DB.

Each test gets a fresh SQLite file via `LLM_USAGE_DB_URL`, runs
`bootstrap()` so `pricing_snapshot` is seeded from the vendored JSON,
and exercises one of the four wired-up surface entries:

- `list_providers` tool
- `get_pricing` tool
- `usage://pricing_table` resource
- `usage://recent_events` resource

Tools are async (`@server.tool()` decorates `async def`); resources are
sync. Tools are called via `asyncio.run` to avoid pulling in
`pytest-asyncio` just for these four entries.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

import llm_usage.mcp.server as server_module
from llm_usage.bootstrap import bootstrap
from llm_usage.core.db.models import UsageEvent
from llm_usage.core.db.session import get_session


@pytest.fixture
def seeded_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point `LLM_USAGE_DB_URL` at a fresh DB and run `bootstrap()`."""
    db = tmp_path / "usage.db"
    monkeypatch.setenv("LLM_USAGE_DB_URL", f"sqlite:///{db}")
    bootstrap()
    return db


@pytest.fixture
def empty_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point `LLM_USAGE_DB_URL` at a fresh DB with the schema but no rows."""
    db = tmp_path / "usage.db"
    monkeypatch.setenv("LLM_USAGE_DB_URL", f"sqlite:///{db}")
    # Run migrations only; skip pricing materialization so the table is empty.
    from llm_usage.bootstrap import migrate_to_head

    migrate_to_head()
    return db


def _insert_event(**overrides: object) -> None:
    """Insert one `usage_events` row with sensible defaults."""
    defaults: dict[str, object] = {
        "id": "evt-1",
        "timestamp": 1_700_000_000_000,
        "provider": "anthropic",
        "model": "claude-sonnet-4-6",
        "input_tokens": 100,
        "output_tokens": 200,
        "cache_write_tokens": 0,
        "cache_read_tokens": 0,
        "cost_nano_usd": 4_200_000,
        "duration_ms": 500,
        "success": True,
    }
    defaults.update(overrides)
    with get_session() as session:
        session.add(UsageEvent(**defaults))
        session.commit()


# --- list_providers --------------------------------------------------------


def test_list_providers_returns_all_v1_providers(seeded_db: Path) -> None:
    result = asyncio.run(server_module.list_providers())
    names = {entry.name for entry in result.providers}
    assert names == {"anthropic", "openai", "qwen", "deepseek"}


def test_list_providers_openai_compatible_flag(seeded_db: Path) -> None:
    result = asyncio.run(server_module.list_providers())
    flags = {entry.name: entry.openai_compatible for entry in result.providers}
    assert flags == {"anthropic": False, "openai": True, "qwen": True, "deepseek": True}


def test_list_providers_models_are_non_empty_and_sorted(seeded_db: Path) -> None:
    result = asyncio.run(server_module.list_providers())
    for entry in result.providers:
        assert entry.models, f"{entry.name} has no models"
        assert entry.models == sorted(entry.models), f"{entry.name} models not sorted"


def test_list_providers_alphabetical_provider_order(seeded_db: Path) -> None:
    result = asyncio.run(server_module.list_providers())
    names = [entry.name for entry in result.providers]
    assert names == sorted(names)


def test_list_providers_empty_when_pricing_empty(empty_db: Path) -> None:
    result = asyncio.run(server_module.list_providers())
    assert result.providers == []


# --- get_pricing -----------------------------------------------------------


def test_get_pricing_no_filter_returns_every_seeded_row(seeded_db: Path) -> None:
    result = asyncio.run(server_module.get_pricing())
    assert len(result.models) > 0
    providers = {entry.provider for entry in result.models}
    assert providers == {"anthropic", "openai", "qwen", "deepseek"}


def test_get_pricing_filter_by_provider(seeded_db: Path) -> None:
    result = asyncio.run(server_module.get_pricing(provider="anthropic"))
    assert len(result.models) > 0
    assert {entry.provider for entry in result.models} == {"anthropic"}


def test_get_pricing_filter_by_provider_and_model(seeded_db: Path) -> None:
    # Fetch all anthropic models, pick one, then verify the targeted query.
    all_anthropic = asyncio.run(server_module.get_pricing(provider="anthropic"))
    sample_model = all_anthropic.models[0].model

    result = asyncio.run(server_module.get_pricing(provider="anthropic", model=sample_model))
    assert len(result.models) == 1
    assert result.models[0].provider == "anthropic"
    assert result.models[0].model == sample_model


def test_get_pricing_unknown_model_returns_empty(seeded_db: Path) -> None:
    result = asyncio.run(server_module.get_pricing(provider="anthropic", model="does-not-exist"))
    assert result.models == []


def test_get_pricing_sorted_by_provider_then_model(seeded_db: Path) -> None:
    result = asyncio.run(server_module.get_pricing())
    keys = [(entry.provider, entry.model) for entry in result.models]
    assert keys == sorted(keys)


def test_get_pricing_fields_populated(seeded_db: Path) -> None:
    """Every result row has the required fields and a positive fetched_at."""
    result = asyncio.run(server_module.get_pricing())
    for entry in result.models:
        assert entry.input_per_million_usd >= 0
        assert entry.output_per_million_usd >= 0
        assert entry.fetched_at > 0


# --- usage://pricing_table resource ----------------------------------------


def test_pricing_table_resource_is_well_formed_json(seeded_db: Path) -> None:
    body = server_module.pricing_table()
    parsed = json.loads(body)
    assert isinstance(parsed, list)
    assert len(parsed) > 0
    assert {"provider", "model", "input_per_million_usd"} <= parsed[0].keys()


def test_pricing_table_matches_get_pricing(seeded_db: Path) -> None:
    """Resource and tool must agree — they share `_query_pricing`."""
    resource_rows = json.loads(server_module.pricing_table())
    tool_result = asyncio.run(server_module.get_pricing())
    tool_rows = [entry.model_dump() for entry in tool_result.models]
    assert resource_rows == tool_rows


def test_pricing_table_empty_when_db_empty(empty_db: Path) -> None:
    assert json.loads(server_module.pricing_table()) == []


# --- usage://recent_events resource ----------------------------------------


def test_recent_events_empty_on_fresh_db(seeded_db: Path) -> None:
    assert json.loads(server_module.recent_events()) == []


def test_recent_events_returns_inserted_row(seeded_db: Path) -> None:
    _insert_event(id="evt-1", timestamp=1_700_000_000_000, project="my-project")

    rows = json.loads(server_module.recent_events())
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == "evt-1"
    assert row["provider"] == "anthropic"
    assert row["project"] == "my-project"
    assert row["cost_nano_usd"] == 4_200_000
    # cost_usd is the float-USD companion of cost_nano_usd.
    assert row["cost_usd"] == pytest.approx(0.0042)


def test_recent_events_orders_latest_first(seeded_db: Path) -> None:
    _insert_event(id="old", timestamp=1_700_000_000_000)
    _insert_event(id="new", timestamp=1_800_000_000_000)
    _insert_event(id="middle", timestamp=1_750_000_000_000)

    rows = json.loads(server_module.recent_events())
    ids = [row["id"] for row in rows]
    assert ids == ["new", "middle", "old"]


def test_recent_events_caps_at_limit(seeded_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Insert more than the limit; verify the resource truncates."""
    # Patch the limit down so the test stays fast and doesn't insert 51 rows.
    monkeypatch.setattr(server_module, "_RECENT_EVENTS_LIMIT", 3)
    for i in range(5):
        _insert_event(id=f"evt-{i}", timestamp=1_700_000_000_000 + i)

    rows = json.loads(server_module.recent_events())
    assert len(rows) == 3
    # Truncation keeps the *latest* — IDs 4, 3, 2.
    assert [row["id"] for row in rows] == ["evt-4", "evt-3", "evt-2"]


def test_recent_events_parses_tags_and_metadata(seeded_db: Path) -> None:
    """`tags`/`metadata` are JSON-encoded TEXT in SQLite; resource parses them back."""
    _insert_event(
        id="evt-tagged",
        tags='["prod", "billing"]',
        event_metadata='{"trace_id": "abc-123"}',
    )

    rows = json.loads(server_module.recent_events())
    assert rows[0]["tags"] == ["prod", "billing"]
    assert rows[0]["metadata"] == {"trace_id": "abc-123"}


def test_recent_events_null_tags_and_metadata_stay_null(seeded_db: Path) -> None:
    _insert_event(id="evt-bare", tags=None, event_metadata=None)

    rows = json.loads(server_module.recent_events())
    assert rows[0]["tags"] is None
    assert rows[0]["metadata"] is None
