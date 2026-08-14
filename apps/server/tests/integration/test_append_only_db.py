"""Append-only trigger backstop — behaviour against a real Postgres (ADR-0004 part 3).

Proves the *exact* DDL the migrations install actually blocks ``UPDATE``/``DELETE`` while leaving
``INSERT``/``SELECT`` working. Runs entirely inside a throwaway schema (created and dropped here) so
it never touches real tables or the dev data. Skips when no Postgres is reachable, mirroring the
Vault contract test — so a keyless CI run never fails, while a provisioned DB gives real coverage.
"""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from sentinelai.platform.config import settings
from sentinelai.platform.db.append_only import (
    create_reject_function_sql,
    create_trigger_sql,
)

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


async def test_append_only_trigger_blocks_update_and_delete() -> None:
    if not await _reachable(_URL):
        pytest.skip(
            f"no Postgres reachable at {_URL.split('@')[-1]} — set TEST_DATABASE_URL to run"
        )

    engine = create_async_engine(_URL)
    schema = f"test_ao_{uuid.uuid4().hex[:8]}"
    q = f'"{schema}".ledger'
    try:
        # Arrange: a throwaway table guarded by the SAME DDL the migrations install.
        async with engine.begin() as conn:
            await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
            await conn.execute(text(f"CREATE TABLE {q} (id int PRIMARY KEY, v text NOT NULL)"))
            await conn.execute(text(create_reject_function_sql(schema)))
            await conn.execute(text(create_trigger_sql(schema, "ledger")))
            await conn.execute(text(f"INSERT INTO {q} (id, v) VALUES (1, 'original')"))

        # UPDATE must be rejected by the trigger.
        with pytest.raises(DBAPIError) as update_err:
            async with engine.begin() as conn:
                await conn.execute(text(f"UPDATE {q} SET v = 'tampered' WHERE id = 1"))
        assert "append-only violation" in str(update_err.value)

        # DELETE must be rejected by the trigger.
        with pytest.raises(DBAPIError) as delete_err:
            async with engine.begin() as conn:
                await conn.execute(text(f"DELETE FROM {q} WHERE id = 1"))
        assert "append-only violation" in str(delete_err.value)

        # INSERT + SELECT remain allowed, and the original row is untouched.
        async with engine.begin() as conn:
            await conn.execute(text(f"INSERT INTO {q} (id, v) VALUES (2, 'appended')"))
            rows = (await conn.execute(text(f"SELECT id, v FROM {q} ORDER BY id"))).all()
        assert rows == [(1, "original"), (2, "appended")]
    finally:
        async with engine.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await engine.dispose()
