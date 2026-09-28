"""Idempotency persistence — ADR-0012 §1/§2.

Repositories persist only (guide Part 3). The decisions — replay, conflict, or proceed — belong to
:mod:`sentinelai.platform.idempotency.guard`; this layer answers "is there a row", "claim this
one", "record what it produced", and "delete what has expired".

Every write flushes rather than commits: the transaction belongs to the entrypoint (ADR-0005), and
that is the whole point here. The claim and the business write share one transaction, so a request
that fails takes its claim down with it and the retry is free to proceed.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, cast
from uuid import UUID

from sqlalchemy import CursorResult, delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from sentinelai.platform.idempotency.models import (
    STATE_CLAIMED,
    STATE_COMPLETED,
    IdempotencyKey,
)


class IdempotencyRepository:
    """Reads and writes ``platform.idempotency_keys``."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, *, principal_id: UUID, key: str, path: str) -> IdempotencyKey | None:
        """Return the row for this claim tuple, expired or not.

        **Does not filter on expiry**, mirroring ``SessionRepository.get_active_by_token``: the
        caller decides validity. That split is what lets the guard distinguish "no such key" from
        "expired, so treat it as new" — which are the same answer to a client and two different
        rows to an operator reading the table.
        """
        result = await self._session.execute(
            select(IdempotencyKey).where(
                IdempotencyKey.principal_id == principal_id,
                IdempotencyKey.idempotency_key == key,
                IdempotencyKey.path == path,
            )
        )
        return result.scalar_one_or_none()

    async def claim(
        self,
        *,
        principal_id: UUID,
        key: str,
        method: str,
        path: str,
        fingerprint: str,
        now: datetime,
        ttl_seconds: int,
    ) -> IdempotencyKey:
        """Insert a ``claimed`` row for this request, flushed so the unique index takes effect.

        The flush is load-bearing rather than tidy: it is what pushes the index entry down to
        Postgres, which is what makes a concurrent duplicate wait. Deferring to session teardown
        would let two requests both pass their pre-check and both run the business logic.

        Raises ``IntegrityError`` when another transaction already committed this claim. The caller
        must issue it inside a SAVEPOINT (``session.begin_nested``) — an ``IntegrityError`` poisons
        the transaction it happens in, and the outer one still has a response to replay.
        """
        row = IdempotencyKey(
            idempotency_key=key,
            principal_id=principal_id,
            method=method,
            path=path,
            request_fingerprint=fingerprint,
            state=STATE_CLAIMED,
            created_at=now,
            expires_at=now + timedelta(seconds=ttl_seconds),
            replay_count=0,
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def complete(
        self,
        row: IdempotencyKey,
        *,
        status_code: int,
        headers: dict[str, Any],
        body: bytes,
    ) -> None:
        """Record what the claimed request produced, in the request's own transaction.

        ADR-0012 §2(c): the response is stored atomically with the business write it describes. A
        separate transaction here would admit the state this design exists to exclude — a committed
        effect whose response was never recorded, so the retry re-executes it.
        """
        row.state = STATE_COMPLETED
        row.response_status = status_code
        row.response_headers = headers
        row.response_body = body
        await self._session.flush()

    async def drop(self, row: IdempotencyKey) -> None:
        """Delete a claim without completing it.

        For a handler that returned a failure *without* raising, so ADR-0005's boundary will commit
        rather than roll back. Keeping the claim would cache nothing while still blocking every
        retry of that key until the TTL expired — turning a transient failure into a permanent one.
        """
        await self._session.delete(row)
        await self._session.flush()

    async def note_replay(self, row: IdempotencyKey) -> None:
        """Increment the replay counter. Support telemetry only; no decision reads it."""
        await self._session.execute(
            update(IdempotencyKey)
            .where(IdempotencyKey.idempotency_id == row.idempotency_id)
            .values(replay_count=IdempotencyKey.replay_count + 1)
            .execution_options(synchronize_session=False)
        )

    async def purge_expired(self, *, now: datetime) -> int:
        """Delete every row past its ``expires_at``; returns how many (ADR-0012 §3).

        Deletes rather than archives. A row's whole purpose is to answer a retry inside the window;
        past it there is nothing to answer, and the audit trail of what actually happened lives in
        ``platform.audit_log``, which is append-only and not this table's job to duplicate.
        """
        result = await self._session.execute(
            delete(IdempotencyKey).where(IdempotencyKey.expires_at <= now)
        )
        # `Result` is the statically-declared return type; a DML statement actually yields a
        # `CursorResult`, which is what carries `rowcount`. Narrowed rather than ignored so the
        # cast is the thing being asserted, not the error.
        return int(cast("CursorResult[Any]", result).rowcount)


__all__ = ["STATE_COMPLETED", "IdempotencyRepository"]
