"""Diagnostic snapshot for the `llm-usage status` CLI.

Read-only inspection of every piece of local state the user might
have a question about: the SQLite DB (path, size, schema rev, event
range), the capture proxy (bind config + a short TCP-probe to see
if anything's listening), per-provider configuration (API key set,
base URL override, how many models in the pricing snapshot), and
the pricing catalog (size + last refresh).

The function is split out from `cli.py` so it can be unit-tested
against tmp-path DBs without involving Typer / CliRunner. The
renderer in `cli_render.py` takes the returned `StatusReport` and
turns it into the human-readable view; `--json` callers consume the
same shape directly.

Design notes captured here so the spec stays close to the code:

- `database=None` / `pricing=None` is the "DB not initialized" case
  (the file doesn't exist yet). `status` should be **observational**
  — running it must never create files. The renderer special-cases
  this state by showing a single hint line rather than empty blocks.
- The proxy probe is a single non-blocking TCP `create_connection`
  with a 0.5s timeout against `127.0.0.1:<port>`. That's enough to
  answer "something is listening on the port"; it does *not* prove
  the listener is our proxy (a stale process could squat on 5525).
  HTTP-level probing is a follow-up.
- `check_proxy=False` short-circuits the probe; the caller wires
  this to a `--no-net` CLI flag so an offline laptop / CI sandbox /
  pytest run doesn't take a 0.5s hit per `status` invocation.
- Schema-behind-head detection compares `alembic_version.version_num`
  against the in-process Alembic config's head revision. The state
  is informational — the next `proxy` or `mcp` boot will migrate
  automatically (`bootstrap()` is idempotent).
"""

from __future__ import annotations

import socket
from importlib import metadata
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import func, select, text
from sqlalchemy.engine.url import make_url

from llm_usage.bootstrap import _find_alembic_root
from llm_usage.config import KNOWN_PROVIDERS, Provider, Settings
from llm_usage.core.db.models import PricingSnapshot, UsageEvent
from llm_usage.core.db.session import get_session
from llm_usage.core.models import (
    StatusDatabase,
    StatusPricing,
    StatusProvider,
    StatusProxy,
    StatusReport,
)

# Lowercase provider names → properly-capitalized brand. Mirrors the
# `_PROVIDER_DISPLAY` map in `cli_render.py` — duplicated rather than
# imported so the diagnostic layer doesn't depend on the renderer.
# Centralizing this is on the backlog (see status doc evaluation).
_PROVIDER_DISPLAY: dict[str, str] = {
    "anthropic": "Anthropic",
    "openai": "OpenAI",
    "qwen": "Qwen",
    "deepseek": "DeepSeek",
}

# Short timeout for the proxy reachability probe. 0.5s is enough for
# a loopback connection to either succeed instantly or be flagged as
# unreachable; longer would make the `status` command feel sluggish
# the moment the proxy isn't running.
_PROXY_PROBE_TIMEOUT_S = 0.5


def collect_status(settings: Settings, *, check_proxy: bool = True) -> StatusReport:
    """Return a fully-populated `StatusReport`.

    `check_proxy=False` skips the TCP probe and reports
    `proxy.reachable=None`. The caller (the CLI `status` command)
    wires `check_proxy=not no_net` so an offline run doesn't pay
    the 0.5s connect timeout.
    """
    db_path = _resolve_db_path(settings.db_url)
    db_initialized = db_path is not None and db_path.exists()

    database: StatusDatabase | None
    pricing: StatusPricing | None
    providers_with_counts: dict[str, int]

    if db_initialized:
        assert db_path is not None
        with get_session() as session:
            database = _collect_database(session, db_path)
            pricing = _collect_pricing(session)
            providers_with_counts = _model_counts_by_provider(session)
    else:
        database = None
        pricing = None
        providers_with_counts = dict.fromkeys(KNOWN_PROVIDERS, 0)

    return StatusReport(
        version=_package_version(),
        database=database,
        proxy=_collect_proxy(settings, check=check_proxy),
        providers=_collect_providers(settings, providers_with_counts),
        pricing=pricing,
    )


# --- database -------------------------------------------------------------


def _resolve_db_path(db_url: str) -> Path | None:
    """Extract the on-disk path from a SQLite URL. Returns `None` for `:memory:`."""
    parsed = make_url(db_url)
    if not parsed.drivername.startswith("sqlite"):
        return None
    if not parsed.database or parsed.database == ":memory:":
        return None
    return Path(parsed.database)


def _collect_database(session: object, db_path: Path) -> StatusDatabase:
    """Read every SQLite stat we report: schema rev + event aggregates."""
    from sqlalchemy.orm import Session

    assert isinstance(session, Session)  # narrowing for mypy without circular imports

    size_bytes = db_path.stat().st_size
    rev = _current_schema_revision(session)
    at_head = _schema_at_head(rev)
    event_count = int(session.execute(select(func.count()).select_from(UsageEvent)).scalar() or 0)
    oldest = newest = None
    if event_count > 0:
        bounds = session.execute(
            select(func.min(UsageEvent.timestamp), func.max(UsageEvent.timestamp))
        ).one()
        oldest = int(bounds[0])
        newest = int(bounds[1])

    return StatusDatabase(
        path=str(db_path),
        size_bytes=size_bytes,
        schema_revision=rev,
        schema_at_head=at_head,
        event_count=event_count,
        oldest_event_ms=oldest,
        newest_event_ms=newest,
    )


def _current_schema_revision(session: object) -> str | None:
    """Read `alembic_version.version_num` or return `None` if the table is empty.

    Alembic's `alembic_version` is a single-row table written by every
    `upgrade` invocation. Reading it directly avoids spinning up
    Alembic's `MigrationContext`, which is slower and more side-effect-y.
    """
    from sqlalchemy.orm import Session

    assert isinstance(session, Session)
    try:
        rev = session.execute(text("SELECT version_num FROM alembic_version LIMIT 1")).scalar()
    except Exception:
        return None
    return str(rev) if rev else None


def _schema_at_head(rev: str | None) -> bool:
    """True iff `rev` matches the in-process Alembic script directory's head.

    Returns `False` for an empty / missing version table so the
    renderer can flag "needs a boot to migrate." Alembic's head
    revision comes from the local migrations directory — same source
    `bootstrap()` uses on startup.
    """
    if rev is None:
        return False
    try:
        cfg = Config(str(_find_alembic_root() / "alembic.ini"))
        head = ScriptDirectory.from_config(cfg).get_current_head()
    except Exception:
        return False
    return rev == head


# --- pricing --------------------------------------------------------------


def _collect_pricing(session: object) -> StatusPricing:
    from sqlalchemy.orm import Session

    assert isinstance(session, Session)
    model_count = int(
        session.execute(select(func.count()).select_from(PricingSnapshot)).scalar() or 0
    )
    provider_count = int(
        session.execute(select(func.count(func.distinct(PricingSnapshot.provider)))).scalar() or 0
    )
    newest = session.execute(select(func.max(PricingSnapshot.fetched_at))).scalar()
    return StatusPricing(
        model_count=model_count,
        provider_count=provider_count,
        newest_fetched_at_ms=int(newest) if newest is not None else None,
    )


def _model_counts_by_provider(session: object) -> dict[str, int]:
    """`pricing_snapshot` rows grouped by provider, as a name → count map.

    Returned with `KNOWN_PROVIDERS` defaulted to 0 so the renderer
    can iterate the union without `KeyError`. A provider whose
    pricing hasn't been seeded reports 0, which is the correct
    "no models known here" answer.
    """
    from sqlalchemy.orm import Session

    assert isinstance(session, Session)
    # `KNOWN_PROVIDERS` is `frozenset[Provider]` (a Literal type); the
    # explicit `dict[str, int]` annotation widens the key type so the
    # `[provider]` assignment below from a plain SQL `str` doesn't
    # narrow back to Literal under mypy.
    counts: dict[str, int] = dict.fromkeys(KNOWN_PROVIDERS, 0)
    rows = session.execute(
        select(PricingSnapshot.provider, func.count()).group_by(PricingSnapshot.provider)
    ).all()
    for provider, count in rows:
        counts[provider] = int(count)
    return counts


# --- proxy ----------------------------------------------------------------


def _collect_proxy(settings: Settings, *, check: bool) -> StatusProxy:
    """Bind config + a short loopback TCP probe.

    `check=False` reports `reachable=None`. Otherwise the probe is a
    non-blocking `create_connection` with `_PROXY_PROBE_TIMEOUT_S` —
    short enough that an unreachable port doesn't make `status` feel
    slow, long enough that a real listener replies within it.
    """
    host = "127.0.0.1"
    port = settings.proxy_port
    if not check:
        return StatusProxy(host=host, port=port, reachable=None)
    try:
        with socket.create_connection((host, port), timeout=_PROXY_PROBE_TIMEOUT_S):
            reachable = True
    except OSError:
        reachable = False
    return StatusProxy(host=host, port=port, reachable=reachable)


# --- providers -------------------------------------------------------------


def _collect_providers(settings: Settings, model_counts: dict[str, int]) -> list[StatusProvider]:
    """One `StatusProvider` per `KNOWN_PROVIDERS`, sorted by display name."""
    out = [
        StatusProvider(
            name=name,
            display_name=_PROVIDER_DISPLAY.get(name, name.title()),
            key_set=settings.api_key_for(_provider_literal(name)) is not None,
            base_url=settings.base_url_for(_provider_literal(name)),
            model_count=model_counts.get(name, 0),
        )
        for name in sorted(KNOWN_PROVIDERS)
    ]
    return out


def _provider_literal(name: str) -> Provider:
    """Narrow `str → Provider` for mypy at the `Settings` call boundary.

    `KNOWN_PROVIDERS` is `frozenset[Provider]` but iterating it yields
    `str` from mypy's perspective. The assertion is the runtime guard
    paired with the type narrowing; if a non-Provider name ever
    sneaks into `KNOWN_PROVIDERS`, the assert fires before the type
    error.
    """
    if name not in KNOWN_PROVIDERS:
        raise AssertionError(f"unknown provider {name!r}")
    return name


# --- package metadata ------------------------------------------------------


def _package_version() -> str:
    """Read `version` from the installed package metadata.

    Single source of truth: `[project] version = "0.1.0"` in
    `pyproject.toml`. Falls back to `"unknown"` for an editable-but-
    uninstalled checkout where `importlib.metadata` can't find the
    distribution.
    """
    try:
        return metadata.version("llm-usage-mcp")
    except metadata.PackageNotFoundError:
        return "unknown"


__all__ = ["collect_status"]
