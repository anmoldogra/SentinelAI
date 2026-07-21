"""ingestion event wiring — event-driven-architecture.md §25.2.

``ingestion`` is a PURE PUBLISHER in Phase 1 (Consumed: none) — its work is driven
by direct API calls, not other modules' events. Published events are emitted from
``service.py`` via ``uow.outbox.publish``; the constants below are the catalog names.
"""

from __future__ import annotations

from sentinelai.platform.events.dispatcher import EventDispatcher

SCHEMA = "ingestion"

# Published (§25.2).
EVENT_EVIDENCE_INGESTED = "evidence.ingested"
EVENT_EVIDENCE_SUPERSEDED = "evidence.superseded"
EVENT_EVIDENCE_VALIDATION_FAILED = "evidence.validation_failed"


def register_consumers(dispatcher: EventDispatcher) -> None:
    """Intentionally empty: ingestion consumes no events (§25.2, pure publisher)."""
    return None
