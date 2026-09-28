"""Event envelope — event-driven-architecture.md §9, §11.

The single shape every event carries, in-process (Phase 1) and over Redpanda
(Phase 3+) without change. It is the full column set of every module's
``outbox_events`` table, plus the correlation/causation/trace triad (§11).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import RowMapping


@dataclass(frozen=True, slots=True)
class EventEnvelope:
    """Immutable value object handed to every consumer handler."""

    event_id: UUID
    event_type: str
    event_version: str
    aggregate_type: str
    aggregate_id: UUID
    payload: dict[str, Any]
    correlation_id: UUID
    causation_id: UUID | None
    trace_id: str | None
    actor_type: str
    actor_ref: UUID | None
    occurred_at: datetime
    dispatch_status: str
    attempt_count: int
    # ADR-0007 (Wave 2.3). ``None`` for a row written before signing existed, or by a publisher with
    # no signing identity wired. The dispatcher distinguishes that from a signature that fails to
    # verify, because permissive mode tolerates the first and never the second.
    signature: bytes | None = None
    key_id: str | None = None
    sig_alg: str | None = None

    @classmethod
    def from_row(cls, row: RowMapping) -> EventEnvelope:
        """Build an envelope from a fetched ``outbox_events`` row mapping."""
        return cls(
            event_id=row["event_id"],
            event_type=row["event_type"],
            event_version=row["event_version"],
            aggregate_type=row["aggregate_type"],
            aggregate_id=row["aggregate_id"],
            payload=row["payload"],
            correlation_id=row["correlation_id"],
            causation_id=row["causation_id"],
            trace_id=row["trace_id"],
            actor_type=row["actor_type"],
            actor_ref=row["actor_ref"],
            occurred_at=row["occurred_at"],
            dispatch_status=row["dispatch_status"],
            attempt_count=row["attempt_count"],
            # `.get` rather than `[...]`: a projection that selected only the dispatch columns would
            # otherwise raise here, and an envelope built without signature fields is legitimate.
            signature=row.get("signature"),
            key_id=row.get("key_id"),
            sig_alg=row.get("sig_alg"),
        )
