"""TTL sweep for the idempotency store — ADR-0012 §3.

Without this the table grows without bound: every keyed request leaves a row, and nothing else ever
deletes one. The read path already ignores an expired row (and deletes it in passing when a client
reuses that key), so this job is about disk and index size, not correctness — which is why a missed
run is uninteresting and a failed one simply waits for the next.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sentinelai.platform.db.session import async_session_factory
from sentinelai.platform.idempotency.repository import IdempotencyRepository
from sentinelai.platform.logging import log


async def purge_expired_idempotency_keys(ctx: dict[str, Any]) -> int:
    """Delete every idempotency record past its ``expires_at``; returns how many.

    One statement, committed once. ``expires_at`` is a stored column rather than a computed
    ``created_at + interval``, so changing the configured TTL never retroactively deletes keys that
    were written under the old window — a row expires when it was always going to.
    """
    session_factory = ctx.get("session_factory") or async_session_factory
    now = datetime.now(UTC)
    async with session_factory() as session:
        deleted = await IdempotencyRepository(session).purge_expired(now=now)
        await session.commit()
    log.info("idempotency_keys_purged", deleted=deleted)
    return deleted


__all__ = ["purge_expired_idempotency_keys"]
