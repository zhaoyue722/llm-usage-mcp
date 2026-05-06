"""Alembic migration environment.

The database URL is read from `LLM_USAGE_DB_URL`, defaulting to the spec's
`~/.llm-usage/usage.db`. SQLite gets `render_as_batch=True` so future ALTER
TABLE migrations work via Alembic's batch mode.
"""

from __future__ import annotations

import os
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

from llm_usage.core import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)


def _resolve_db_url() -> str:
    env_url = os.environ.get("LLM_USAGE_DB_URL")
    if env_url:
        return env_url
    default_path = Path.home() / ".llm-usage" / "usage.db"
    return f"sqlite:///{default_path}"


config.set_main_option("sqlalchemy.url", _resolve_db_url())

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
