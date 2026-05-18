"""Alembic migration environment.

The database URL is read from `LLM_USAGE_DB_URL`, defaulting to the spec's
`~/.llm-usage/usage.db`. SQLite gets `render_as_batch=True` so future ALTER
TABLE migrations work via Alembic's batch mode.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from llm_usage.core import Base, resolve_db_url

config = context.config

if config.config_file_name is not None:
    # `disable_existing_loggers=False` keeps our app's loggers (and
    # pytest's caplog handler) attached after fileConfig runs.
    # Default behavior would silently kill any logger configured by
    # the caller before `bootstrap()` reaches alembic — including
    # test capture handlers — so warnings emitted later in the boot
    # sequence become invisible.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

config.set_main_option("sqlalchemy.url", resolve_db_url())

target_metadata = Base.metadata


def _is_sqlite(url: str) -> bool:
    return url.startswith("sqlite")


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url") or ""
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=_is_sqlite(url),
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    section = config.get_section(config.config_ini_section, {})
    connectable = engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=connection.dialect.name == "sqlite",
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
