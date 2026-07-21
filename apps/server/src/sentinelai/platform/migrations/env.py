"""Alembic environment for the ``platform`` schema (guide Part 4).

Per-module migration ownership: this env manages ONLY the ``platform`` schema
and keeps its own ``alembic_version`` table INSIDE that schema. It registers only
this module's models on ``Base.metadata`` (selective import), so autogenerate is
scoped to this schema and cannot touch another module's tables (database-design §11).

Alembic runs synchronously; the async ``+asyncpg`` URL is swapped to a sync driver.
The shared migration runner that applies all modules in DAG order is wired in a
later phase (guide Part 4 / Part 20).
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

import sentinelai.platform.auth.models  # noqa: F401  (registers models on Base.metadata)
from sentinelai.platform.config import settings
from sentinelai.platform.db.base import Base

VERSION_TABLE_SCHEMA = "platform"

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _sync_url() -> str:
    """Alembic is synchronous — use a sync driver regardless of the app's async URL."""
    return settings.database_url.replace("+asyncpg", "+psycopg")


def run_migrations_offline() -> None:
    context.configure(
        url=_sync_url(),
        target_metadata=target_metadata,
        version_table="alembic_version",
        version_table_schema=VERSION_TABLE_SCHEMA,
        include_schemas=True,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = create_engine(_sync_url(), poolclass=pool.NullPool)
    with connectable.connect() as connection:
        # Ensure the module's schema exists before Alembic creates its
        # version table inside it (version_table_schema below).
        connection.exec_driver_sql(f'CREATE SCHEMA IF NOT EXISTS "{VERSION_TABLE_SCHEMA}"')
        connection.commit()
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            version_table="alembic_version",
            version_table_schema=VERSION_TABLE_SCHEMA,
            include_schemas=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
