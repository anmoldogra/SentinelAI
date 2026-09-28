"""The idempotency store's ORM model — ADR-0012 §1, schema ``platform``.

One row per ``(principal, key, path)``, holding the fingerprint of the request that claimed it and,
once the request succeeds, the response to replay. It is the HTTP-boundary analogue of the Inbox
pattern's ``(event_id, handler_name)`` claim (event-driven §16): insert first, act second.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Index, Integer, LargeBinary, SmallInteger, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from sentinelai.platform.db.base import Base

_SCHEMA = "platform"

# ADR-0012 §2's two states. `claimed` is the window between "this request is mine" and "here is
# what it produced"; it is never observed by another transaction, because the claim is inserted in
# the request's own transaction and a concurrent duplicate blocks on the unique index until that
# transaction ends. A row that commits is therefore always `completed`, and a request that fails
# takes its claim down with it — no cleanup path, and no leaked in-flight rows to expire.
STATE_CLAIMED = "claimed"
STATE_COMPLETED = "completed"


class IdempotencyKey(Base):
    """A claimed ``Idempotency-Key`` and the response it produced (api-design.md §2.9)."""

    __tablename__ = "idempotency_keys"
    __table_args__ = (
        # ADR-0012 §1's uniqueness, and the concurrency control. Two simultaneous requests carrying
        # the same key race to INSERT here; Postgres makes the loser wait on the index entry until
        # the winner commits or rolls back, which is §2(d)'s "serialize (row lock)" without any
        # explicit locking. Serializing beats answering 409, because a client that retried after a
        # timeout wants the original answer, not a new error.
        UniqueConstraint("principal_id", "idempotency_key", "path", name="uq_idempotency_claim"),
        # The TTL sweep's index (§3). `expires_at` alone: the purge job deletes by age across every
        # principal, and no read path filters on anything more selective.
        Index("ix_idempotency_keys_expires_at", "expires_at"),
        {"schema": _SCHEMA},
    )

    idempotency_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    # The client-supplied key. Named to avoid colliding with SQL's `key`, which is not reserved in
    # Postgres but is in enough other dialects that it reads as a trap.
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)
    # Scoped per authenticated principal (§3): two clients must never be able to collide on, or
    # read back, each other's keys — a guessed key would otherwise replay someone else's response.
    principal_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    method: Mapped[str] = mapped_column(Text, nullable=False)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    # SHA-256 over the canonical (method, path, principal, body) tuple — see `fingerprint.py`.
    # Stored as hex rather than bytes so a support query can compare it by eye.
    request_fingerprint: Mapped[str] = mapped_column(Text, nullable=False)

    state: Mapped[str] = mapped_column(Text, nullable=False, default=STATE_CLAIMED)

    # All three are NULL while `state = claimed` and set together on completion.
    response_status: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    # Only the headers a replay must reproduce (`ETag`, `Location`), not the whole set: `Date`,
    # `Content-Length` and any per-request correlation header are properties of *this* response and
    # would be wrong to serve again. JSONB rather than text so a header can be read out in SQL.
    response_headers: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    # The response body as sent, verbatim. §2.9 requires the replay be byte-identical ("same status
    # code, same body"), so this is the encoded payload rather than a re-serialized object — a
    # re-serialization could differ in key order or float formatting and break a client's own
    # signature or cache check.
    # `LargeBinary` (bytea), not text: "verbatim" means bytes. Decoding to str and re-encoding
    # would work for every response this API currently produces and would silently corrupt the
    # first one that is not valid UTF-8.
    response_body: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)

    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    # Explicit column rather than `created_at + interval` at query time: the TTL is configurable,
    # and a row must expire on the window that was in force when it was written. Changing the
    # setting must not retroactively resurrect or kill keys already stored.
    expires_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)

    # Kept for the metrics the purge job emits and for support ("how many times did this client
    # retry?"). Not part of any decision — a replay is a replay whether it is the first or the
    # fiftieth.
    replay_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


__all__ = ["STATE_CLAIMED", "STATE_COMPLETED", "IdempotencyKey"]
