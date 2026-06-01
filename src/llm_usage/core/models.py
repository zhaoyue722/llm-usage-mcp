"""Pydantic types for the MCP-tool surface defined in `docs/spec.md`.

These are the public contract any MCP client (Claude Code, Cursor,
custom agents) reads off our tools. Two design rules govern the whole
module:

- **`extra="forbid"` everywhere.** Unknown keys raise instead of being
  silently dropped. A typo in `proovider` should surface, not pass.
- **Result models are frozen; param models are not.** A caller that
  receives a result and mutates it is committing a bug; freezing makes
  that bug loud. Params, by contrast, are often built up incrementally
  in test fixtures or CLI parsers, so they stay mutable.

Money fields use `float` at the API boundary (per spec — `cost_usd:
number`). Storage in `usage_events.cost_nano_usd` stays integer; the
`pricing` module's `usd_to_nano` / `nano_to_usd` helpers cross between.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

# --- shared bases ----------------------------------------------------------


class ParamsBase(BaseModel):
    """Base for tool input types: strict (`extra=forbid`) but mutable."""

    model_config = ConfigDict(extra="forbid")


class ResultBase(BaseModel):
    """Base for tool output types: strict (`extra=forbid`) and frozen.

    Frozen-ness means a caller that receives a result and mutates a
    field gets a clear `ValidationError`. The MCP server should never
    mutate a result either; we always construct a fresh one.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)


# --- enum-shaped fields ----------------------------------------------------

GroupBy = Literal["provider", "model", "project", "tag", "day"]
Period = Literal["today", "week", "month", "year"]


# --- record_usage ----------------------------------------------------------


class RecordUsageParams(ParamsBase):
    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0
    duration_ms: int | None = None
    success: bool = True
    error_type: str | None = None
    request_id: str | None = None
    project: str | None = None
    tags: list[str] | None = None
    metadata: dict[str, Any] | None = None


class RecordUsageResult(ResultBase):
    id: str
    cost_usd: float
    warning: str | None


# --- query_spend -----------------------------------------------------------


class SpendFilter(ParamsBase):
    """Optional filters narrowing the `query_spend` window."""

    provider: str | None = None
    model: str | None = None
    project: str | None = None


class QuerySpendParams(ParamsBase):
    start: str | None = None  # ISO-8601
    end: str | None = None  # ISO-8601
    group_by: GroupBy = "provider"
    filter: SpendFilter | None = None
    # Off by default: failure rows (e.g. streams that died mid-flight
    # with partial counts) are excluded from totals and groups so a
    # dashboard built on `query_spend` doesn't mix maybe-billed-maybe-
    # not numbers into "how much did I spend." Pass `True` to bring
    # them back in.
    include_failed: bool = False


class SpendGroup(ResultBase):
    key: str
    cost_usd: float
    calls: int
    input_tokens: int
    output_tokens: int


class QuerySpendResult(ResultBase):
    total_cost_usd: float
    total_calls: int
    total_input_tokens: int
    total_output_tokens: int
    groups: list[SpendGroup]


# --- compare_providers -----------------------------------------------------


class CompareProvidersParams(ParamsBase):
    expected_input_tokens: int
    expected_output_tokens: int
    models: list[str] | None = None


class RankedEntry(ResultBase):
    provider: str
    model: str
    cost_usd: float
    relative_cost_pct: float
    # Per-row note slot from the spec (`notes: string|null`). v1 never
    # writes a value here — left in the schema so a future per-model
    # caveat (e.g., "tiered pricing approximated") doesn't require a
    # schema change. Always `None` today.
    notes: str | None


class CompareProvidersResult(ResultBase):
    ranked: list[RankedEntry]


# --- recommend_provider ----------------------------------------------------


class RecommendProviderParams(ParamsBase):
    task_description: str
    expected_input_tokens: int | None = None
    expected_output_tokens: int | None = None
    budget_usd: float | None = None


class RecommendProviderResult(ResultBase):
    provider: str
    model: str
    estimated_cost_usd: float
    reasoning: str


# --- get_pricing -----------------------------------------------------------


class GetPricingParams(ParamsBase):
    provider: str | None = None
    model: str | None = None


class PricingEntry(ResultBase):
    provider: str
    model: str
    input_per_million_usd: float
    output_per_million_usd: float
    cache_write_per_million_usd: float | None
    cache_read_per_million_usd: float | None
    fetched_at: int  # ms epoch


class GetPricingResult(ResultBase):
    models: list[PricingEntry]


# --- usage_summary ---------------------------------------------------------


class UsageSummaryParams(ParamsBase):
    period: Period = "week"
    # Symmetric with `QuerySpendParams.include_failed`: by default the
    # summary's totals, top-N rollups, and `largest_call` all exclude
    # failure rows so the headline numbers don't drift on partial
    # streams.
    include_failed: bool = False


class TopProvider(ResultBase):
    provider: str
    cost_usd: float
    pct: float


class TopModel(ResultBase):
    model: str
    cost_usd: float
    pct: float


class LargestCall(ResultBase):
    id: str
    model: str
    cost_usd: float
    timestamp: int  # ms epoch — kept as integer so MCP clients don't have to parse


class UsageSummaryResult(ResultBase):
    period: str
    total_cost_usd: float
    call_count: int
    top_providers: list[TopProvider]
    top_models: list[TopModel]
    # `None` when the window has zero recorded events — required-field
    # `LargestCall` would otherwise make `usage_summary` un-constructable
    # on a fresh DB.
    largest_call: LargestCall | None


# --- list_providers --------------------------------------------------------


class ListProvidersParams(ParamsBase):
    """No parameters; the empty model is the contract."""


class ProviderEntry(ResultBase):
    name: str
    models: list[str]
    openai_compatible: bool


class ListProvidersResult(ResultBase):
    providers: list[ProviderEntry]


# --- status (CLI diagnostic, no MCP tool) ----------------------------------
# `llm-usage status` reports a snapshot of "is everything wired up?": DB,
# proxy reachability, per-provider config, pricing freshness. Nested
# `StatusReport` shape so `--json` consumers can read each leg without
# string-parsing the human view.


class StatusDatabase(ResultBase):
    """SQLite stats. `None` on the parent when the DB hasn't been created yet."""

    path: str
    size_bytes: int
    schema_revision: str | None  # `None` if the alembic_version table is empty.
    schema_at_head: bool
    event_count: int
    oldest_event_ms: int | None
    newest_event_ms: int | None


class StatusProxy(ResultBase):
    """Capture-proxy binding + reachability probe.

    `reachable=None` means the probe was skipped (the `--no-net` CLI
    flag, or any other reason the caller doesn't want a network
    syscall during diagnostics — offline laptop, CI, etc.).
    """

    host: str
    port: int
    reachable: bool | None


class StatusProvider(ResultBase):
    """Per-provider configuration view. One entry per `KNOWN_PROVIDERS`."""

    name: str  # lowercase DB-style: "anthropic"
    display_name: str  # branded: "Anthropic", "OpenAI"
    key_set: bool
    base_url: str
    model_count: int  # rows in `pricing_snapshot` for this provider.


class StatusPricing(ResultBase):
    """`pricing_snapshot` summary. `None` when the DB hasn't been created yet."""

    model_count: int
    provider_count: int
    newest_fetched_at_ms: int | None


class StatusReport(ResultBase):
    """Top-level result of `llm-usage status`.

    `database` and `pricing` are both `None` when the DB file doesn't
    exist yet — running `status` before `proxy` or `mcp` has ever
    booted (so `bootstrap()` hasn't run) is a legitimate state. The
    renderer shows a single "database not initialized" line in that
    case rather than fake-empty Database / Pricing blocks.
    """

    version: str
    database: StatusDatabase | None
    proxy: StatusProxy
    providers: list[StatusProvider]
    pricing: StatusPricing | None


__all__ = [
    "CompareProvidersParams",
    "CompareProvidersResult",
    "GetPricingParams",
    "GetPricingResult",
    "GroupBy",
    "LargestCall",
    "ListProvidersParams",
    "ListProvidersResult",
    "ParamsBase",
    "Period",
    "PricingEntry",
    "ProviderEntry",
    "QuerySpendParams",
    "QuerySpendResult",
    "RankedEntry",
    "RecommendProviderParams",
    "RecommendProviderResult",
    "RecordUsageParams",
    "RecordUsageResult",
    "ResultBase",
    "SpendFilter",
    "SpendGroup",
    "StatusDatabase",
    "StatusPricing",
    "StatusProvider",
    "StatusProxy",
    "StatusReport",
    "TopModel",
    "TopProvider",
    "UsageSummaryParams",
    "UsageSummaryResult",
]
