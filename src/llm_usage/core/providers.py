"""Provider catalog snapshot for the `llm-usage providers` CLI.

Read-only inspection of the provider configuration the user has wired
up locally: which keys are present in the environment, which base URLs
are in effect (default vs override), whether the provider speaks
OpenAI's `/v1/chat/completions` wire format, and which models the
local `pricing_snapshot` knows about.

Split out from `cli.py` so it can be unit-tested against tmp-path DBs
without going through Typer's `CliRunner`. The renderer in
`cli_render.format_providers` consumes the returned `ProvidersReport`
for the human view; `--json` callers see the same Pydantic shape.

Design notes pinned here so the contract stays close to the code:

- Iterates `KNOWN_PROVIDERS`, **not** the providers that happen to
  have rows in `pricing_snapshot`. A provider with no seeded pricing
  still shows up with `models=[]` so the user sees its config (key /
  URL) — the MCP `list_providers` tool, by contrast, only surfaces
  priced providers because its purpose is "what can I call?".
- Observational. If the SQLite file doesn't exist yet (no boot has
  happened), the function returns provider rows with empty model
  lists rather than creating the DB. Same rule as `collect_status`.
- `OPENAI_COMPATIBLE` lives here, not in `mcp/server.py`, so both
  surfaces (MCP `list_providers` and CLI `providers`) read it from
  one source.
"""

from __future__ import annotations

from typing import Final

from sqlalchemy import select
from sqlalchemy.engine.url import make_url

from llm_usage.config import KNOWN_PROVIDERS, Provider, Settings
from llm_usage.core.db.models import PricingSnapshot
from llm_usage.core.db.session import get_session
from llm_usage.core.models import ProviderRow, ProvidersReport

# Wire-format flag per provider. Anthropic uses its own `/v1/messages`
# shape; OpenAI, Qwen (via DashScope's compatible-mode endpoint), and
# DeepSeek all speak OpenAI's `/v1/chat/completions` format. This is
# static metadata, so it lives in code rather than in the DB. Single
# source of truth — `mcp/server.py` imports this map.
OPENAI_COMPATIBLE: Final[dict[str, bool]] = {
    "anthropic": False,
    "openai": True,
    "qwen": True,
    "deepseek": True,
}

# Lowercase DB names → branded display strings. Mirrors the same map
# in `cli_render.py` and `core/diagnostics.py`; consolidation is on
# the backlog (`docs/backlog.md` — see status doc evaluation).
_PROVIDER_DISPLAY: Final[dict[str, str]] = {
    "anthropic": "Anthropic",
    "openai": "OpenAI",
    "qwen": "Qwen",
    "deepseek": "DeepSeek",
}


def collect_providers(settings: Settings) -> ProvidersReport:
    """Return a `ProvidersReport` covering every `KNOWN_PROVIDERS` entry.

    Reads model lists from `pricing_snapshot` when the DB exists, an
    empty list otherwise. Rows are sorted by display name so the
    output reads alphabetically regardless of insertion order.
    """
    models_by_provider = _model_lists_by_provider(settings)

    rows = [
        ProviderRow(
            name=name,
            display_name=_PROVIDER_DISPLAY.get(name, name.title()),
            openai_compatible=OPENAI_COMPATIBLE.get(name, False),
            key_set=settings.api_key_for(_provider_literal(name)) is not None,
            base_url=settings.base_url_for(_provider_literal(name)),
            models=models_by_provider.get(name, []),
        )
        for name in KNOWN_PROVIDERS
    ]
    rows.sort(key=lambda r: r.display_name)
    return ProvidersReport(providers=rows)


def _model_lists_by_provider(settings: Settings) -> dict[str, list[str]]:
    """`pricing_snapshot` rows grouped by provider, sorted within each group.

    Returns an empty dict when the SQLite file doesn't exist yet —
    `status` taught us that opening a session against a missing path
    creates the file, which would break the "observational" rule. We
    short-circuit via the URL inspection instead of relying on
    SQLAlchemy to error out cleanly.
    """
    if not _db_exists(settings.db_url):
        return {}

    stmt = select(PricingSnapshot.provider, PricingSnapshot.model).order_by(
        PricingSnapshot.provider, PricingSnapshot.model
    )
    with get_session() as session:
        rows = session.execute(stmt).all()

    grouped: dict[str, list[str]] = {}
    for provider, model in rows:
        grouped.setdefault(provider, []).append(model)
    return grouped


def _db_exists(db_url: str) -> bool:
    """True iff the SQLite URL points at an on-disk file that exists.

    `:memory:` returns False (no models seeded yet in a transient DB);
    non-SQLite URLs return True (we don't have a path to stat, so
    assume the caller knows what they're doing).
    """
    parsed = make_url(db_url)
    if not parsed.drivername.startswith("sqlite"):
        return True
    if not parsed.database or parsed.database == ":memory:":
        return False
    from pathlib import Path

    return Path(parsed.database).exists()


def _provider_literal(name: str) -> Provider:
    """Narrow `str → Provider` for the typed `Settings` call boundary.

    Mirrors the helper in `core/diagnostics.py`. Asserts so a typo in
    `KNOWN_PROVIDERS` fires loudly instead of slipping past mypy.
    """
    if name not in KNOWN_PROVIDERS:
        raise AssertionError(f"unknown provider {name!r}")
    return name


__all__ = ["OPENAI_COMPATIBLE", "collect_providers"]
