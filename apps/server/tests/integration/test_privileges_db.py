"""Evidentiary-table privileges — behaviour against a real Postgres (ADR-0004 part 2).

Verifies the ADR-0004 privilege model on a throwaway table: under the ``sentinel_append`` role,
``INSERT``/``SELECT`` succeed while ``UPDATE``/``DELETE`` are denied. Applies the *exact* SQL the
migrations use.

The ADR-0004 roles are a deployment prerequisite (cluster-level ``CREATE ROLE``). This test does not
create them — it **skips with a clear reason** when ``sentinel_append`` is not provisioned (and when
no Postgres is reachable), so a keyless/roleless CI run never fails while a provisioned environment
gets real coverage.
"""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from sentinelai.platform.config import settings
from sentinelai.platform.db.privileges import APPEND_ROLE, grant_evidentiary_privileges_sql

_URL = os.getenv("TEST_DATABASE_URL", settings.database_url)


async def _reachable(url: str) -> bool:
    try:
        engine = create_async_engine(url)
        try:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
        finally:
            await engine.dispose()
        return True
    except Exception:
        return False


async def _role_exists(engine: AsyncEngine, role: str) -> bool:
    async with engine.connect() as conn:
        found = (
            await conn.execute(text("SELECT 1 FROM pg_roles WHERE rolname = :role"), {"role": role})
        ).first()
    return found is not None


async def test_append_role_can_insert_select_but_not_update_or_delete() -> None:
    if not await _reachable(_URL):
        pytest.skip(
            f"no Postgres reachable at {_URL.split('@')[-1]} — set TEST_DATABASE_URL to run"
        )

    engine = create_async_engine(_URL)
    try:
        if not await _role_exists(engine, APPEND_ROLE):
            pytest.skip(
                f"role '{APPEND_ROLE}' is not provisioned — ADR-0004 roles are a deployment "
                "prerequisite (part 1 / infra); privilege behaviour cannot be verified without it"
            )

        schema = f"test_priv_{uuid.uuid4().hex[:8]}"
        q = f'"{schema}".ledger'
        async with engine.begin() as conn:
            await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
            await conn.execute(text(f"CREATE TABLE {q} (id int PRIMARY KEY, v text NOT NULL)"))
            # The append role needs USAGE on the schema to reach the table; the table-level
            # INSERT/SELECT grant (below) is what this test isolates. (In production, schema USAGE
            # for the app roles is a Part-2 migration concern — see the note in the test module.)
            await conn.execute(text(f'GRANT USAGE ON SCHEMA "{schema}" TO {APPEND_ROLE}'))
            await conn.execute(text(grant_evidentiary_privileges_sql(schema, "ledger")))
        try:
            # Under the append role: INSERT + SELECT succeed.
            async with engine.begin() as conn:
                await conn.execute(text(f"SET LOCAL ROLE {APPEND_ROLE}"))
                await conn.execute(text(f"INSERT INTO {q} (id, v) VALUES (1, 'original')"))
                count = (await conn.execute(text(f"SELECT count(*) FROM {q}"))).scalar_one()
                assert count == 1

            # Under the append role: UPDATE is denied.
            with pytest.raises(DBAPIError) as update_err:
                async with engine.begin() as conn:
                    await conn.execute(text(f"SET LOCAL ROLE {APPEND_ROLE}"))
                    await conn.execute(text(f"UPDATE {q} SET v = 'tampered' WHERE id = 1"))
            assert "permission denied" in str(update_err.value).lower()

            # Under the append role: DELETE is denied.
            with pytest.raises(DBAPIError) as delete_err:
                async with engine.begin() as conn:
                    await conn.execute(text(f"SET LOCAL ROLE {APPEND_ROLE}"))
                    await conn.execute(text(f"DELETE FROM {q} WHERE id = 1"))
            assert "permission denied" in str(delete_err.value).lower()

            # Under the append role: TRUNCATE is denied (it bypasses the row-level trigger, so the
            # privilege revoke is the control). Same infra assumptions as the checks above.
            with pytest.raises(DBAPIError) as truncate_err:
                async with engine.begin() as conn:
                    await conn.execute(text(f"SET LOCAL ROLE {APPEND_ROLE}"))
                    await conn.execute(text(f"TRUNCATE {q}"))
            assert "permission denied" in str(truncate_err.value).lower()
        finally:
            async with engine.begin() as conn:  # runs as the owner (SET ROLE does not persist)
                await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
    finally:
        await engine.dispose()
