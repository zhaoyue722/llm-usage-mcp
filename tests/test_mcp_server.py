"""Tests for the MCP server stub — registration and schema shape.

The tool/resource bodies are all `NotImplementedError`; what we care
about at this stage is that the **surface** (discoverable by any MCP
client) matches the spec:

- the 7 tools from `docs/spec.md` are registered, with the right names;
- each tool's input schema has exactly the parameter set the spec says;
- each tool has a structured output schema (generated from its
  `*Result` return type, so the wire format is locked);
- both resources are registered with `usage://` URIs.

Schema details (per-field types, enum values, frozen-ness) are covered
in `test_mcp_types.py`; this file only checks that the wiring exists.

Tests drive `await`-able SDK methods through `asyncio.run` to avoid a
`pytest-asyncio` dependency just for the stub layer — there's no
ongoing event-loop state to preserve, each call is independent.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

import llm_usage.mcp.server as server_module
from llm_usage.mcp.server import server


def _list_tools() -> list[Any]:
    return asyncio.run(server.list_tools())


def _list_resources() -> list[Any]:
    return asyncio.run(server.list_resources())


# Tool name -> set of input parameter names the spec mandates.
_EXPECTED_TOOL_INPUTS: dict[str, set[str]] = {
    "record_usage": {
        "provider",
        "model",
        "input_tokens",
        "output_tokens",
        "cache_write_tokens",
        "cache_read_tokens",
        "duration_ms",
        "success",
        "error_type",
        "request_id",
        "project",
        "tags",
        "metadata",
    },
    "query_spend": {"start", "end", "group_by", "filter"},
    "compare_providers": {
        "expected_input_tokens",
        "expected_output_tokens",
        "task_type",
        "models",
        "include_cached_estimate",
    },
    "recommend_provider": {
        "task_description",
        "expected_input_tokens",
        "expected_output_tokens",
        "budget_usd",
        "quality_priority",
    },
    "get_pricing": {"provider", "model"},
    "usage_summary": {"period"},
    "list_providers": set(),
}

_EXPECTED_REQUIRED: dict[str, set[str]] = {
    "record_usage": {"provider", "model", "input_tokens", "output_tokens"},
    "query_spend": set(),
    "compare_providers": {"expected_input_tokens", "expected_output_tokens"},
    "recommend_provider": {"task_description"},
    "get_pricing": set(),
    "usage_summary": set(),
    "list_providers": set(),
}

_EXPECTED_RESOURCES = {"usage://recent_events", "usage://pricing_table"}


# --- tools -----------------------------------------------------------------


def test_seven_tools_registered() -> None:
    tools = _list_tools()
    assert {t.name for t in tools} == set(_EXPECTED_TOOL_INPUTS.keys())


def test_each_tool_input_schema_matches_spec() -> None:
    tools = {t.name: t for t in _list_tools()}
    for name, expected_inputs in _EXPECTED_TOOL_INPUTS.items():
        props = tools[name].inputSchema.get("properties", {})
        assert set(props.keys()) == expected_inputs, name


def test_each_tool_required_fields_match_spec() -> None:
    tools = {t.name: t for t in _list_tools()}
    for name, expected_required in _EXPECTED_REQUIRED.items():
        required = set(tools[name].inputSchema.get("required", []))
        assert required == expected_required, name


def test_each_tool_has_output_schema() -> None:
    """Output schemas come from `*Result` Pydantic return types — pin existence."""
    for t in _list_tools():
        assert t.outputSchema, f"{t.name} has no output schema"


def test_record_usage_input_schema_token_defaults() -> None:
    """Spec: cache_*_tokens default to 0, success defaults to true."""
    tools = {t.name: t for t in _list_tools()}
    props = tools["record_usage"].inputSchema["properties"]
    assert props["cache_write_tokens"]["default"] == 0
    assert props["cache_read_tokens"]["default"] == 0
    assert props["success"]["default"] is True


def test_query_spend_group_by_renders_enum() -> None:
    tools = {t.name: t for t in _list_tools()}
    group_by = tools["query_spend"].inputSchema["properties"]["group_by"]
    assert set(group_by["enum"]) == {"provider", "model", "project", "tag", "day"}
    assert group_by["default"] == "provider"


def test_usage_summary_period_renders_enum_with_week_default() -> None:
    tools = {t.name: t for t in _list_tools()}
    period = tools["usage_summary"].inputSchema["properties"]["period"]
    assert set(period["enum"]) == {"today", "week", "month", "year"}
    assert period["default"] == "week"


# --- resources -------------------------------------------------------------


def test_two_resources_registered() -> None:
    uris = {str(r.uri) for r in _list_resources()}
    assert uris == _EXPECTED_RESOURCES


# --- stub behavior ---------------------------------------------------------


def test_unwired_tool_bodies_still_raise_not_implemented() -> None:
    """The 4 not-yet-wired tools raise.

    `record_usage` is tested in `test_recording.py`; `list_providers`,
    `get_pricing` and the 2 resources in `test_read_path_tools.py` —
    all against real seeded DBs.
    """

    async def run_all() -> None:
        with pytest.raises(NotImplementedError):
            await server_module.query_spend()
        with pytest.raises(NotImplementedError):
            await server_module.compare_providers(expected_input_tokens=0, expected_output_tokens=0)
        with pytest.raises(NotImplementedError):
            await server_module.recommend_provider(task_description="x")
        with pytest.raises(NotImplementedError):
            await server_module.usage_summary()

    asyncio.run(run_all())
