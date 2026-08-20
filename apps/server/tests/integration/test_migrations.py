"""Migration round-trip against a real Postgres (W1-16).

Proves every module's history is **reversible**: `upgrade head` across all nine schemas in
database-design.md §5 DAG order, then `downgrade base` in the exact reverse order, with no
exception from any `downgrade()`. A migration whose downgrade is wrong (a dropped index that was
never created, a trigger dropped before its function, a table dropped out of dependency order)
fails here and nowhere else.

Runs inside a throwaway **database**, not the dev one — every schema is created and dropped, so a
half-applied run can never leave the developer's database in a broken state.

Skips cleanly when no Postgres is reachable (or the sync driver is absent), mirroring the other
integration tests, so a keyless `make check` never fails while a provisioned CI run gets real
coverage. Never fakes a pass.
"""

from __future__ import annotations

import os
import uuid

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

from sentinelai.platform.config import settings
from sentinelai.platform.migrations.currency import (
    MIGRATION_SCHEMAS,
    applied_heads,
    expected_heads,
    script_directory,
)

_URL = os.getenv("TEST_DATABASE_URL", settings.database_url)
_CONNECT_TIMEOUT = 3  # seconds; bounds the reachability probe that decides skip-vs-run


def _sync(url: str) -> str:
    """The sync URL Alembic uses (env.py performs the same rewrite)."""
    return url.replace("+asyncpg", "+psycopg")


def _skip_reason() -> str | None:
    try:
        import psycopg  # noqa: F401
    except ImportError:  # pragma: no cover - environment-dependent
        return "psycopg (sync driver) not installed — required to run Alembic"
    try:
        # An explicit short timeout matters: psycopg otherwise retries a dead endpoint for
        # minutes, which would turn this test's *skip* into a multi-minute stall in `make check`.
        engine = create_engine(_sync(_URL), connect_args={"connect_timeout": _CONNECT_TIMEOUT})
        try:
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))
        finally:
            engine.dispose()
    except Exception:
        return f"no Postgres reachable at {_URL.split('@')[-1]} — set TEST_DATABASE_URL to run"
    return None


def _config(schema: str, url: str) -> Config:
    config = Config()
    config.set_main_option("script_location", str(script_directory(schema)))
    config.set_main_option("sqlalchemy.url", url)
    return config


def test_every_schema_upgrades_to_head_and_downgrades_back_to_base(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reason = _skip_reason()
    if reason:
        pytest.skip(reason)

    admin = create_engine(_sync(_URL), isolation_level="AUTOCOMMIT")
    database = f"sentinelai_migtest_{uuid.uuid4().hex[:8]}"
    try:
        with admin.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{database}"'))
    finally:
        admin.dispose()

    target = _URL.rsplit("/", 1)[0] + f"/{database}"
    # env.py reads the URL from settings at migration time; point it at the throwaway database.
    monkeypatch.setattr(settings, "database_url", target)
    engine = create_engine(_sync(target))

    try:
        # Upgrade: DAG order (platform first, notification last).
        for schema in MIGRATION_SCHEMAS:
            command.upgrade(_config(schema, _sync(target)), "head")

        with engine.connect() as connection:
            for schema in MIGRATION_SCHEMAS:
                assert applied_heads(connection, schema) == expected_heads(schema), (
                    f"{schema} is not at head after upgrade"
                )

        # Downgrade: exact reverse order — a module's tables go before the ones it depends on.
        for schema in reversed(MIGRATION_SCHEMAS):
            command.downgrade(_config(schema, _sync(target)), "base")

        with engine.connect() as connection:
            for schema in MIGRATION_SCHEMAS:
                assert applied_heads(connection, schema) == (), (
                    f"{schema} still records a revision after downgrade to base"
                )
            # Every module's tables are gone; only the (empty) schemas + version tables remain.
            leftover = (
                connection.execute(
                    text(
                        "SELECT table_schema || '.' || table_name FROM information_schema.tables "
                        "WHERE table_schema = ANY(:schemas) AND table_name <> 'alembic_version'"
                    ),
                    {"schemas": list(MIGRATION_SCHEMAS)},
                )
                .scalars()
                .all()
            )
            assert leftover == [], f"tables survived downgrade to base: {leftover}"
    finally:
        engine.dispose()
        admin = create_engine(_sync(_URL), isolation_level="AUTOCOMMIT")
        try:
            with admin.connect() as connection:
                connection.execute(text(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)'))
        finally:
            admin.dispose()
