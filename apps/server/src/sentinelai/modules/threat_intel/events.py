"""threat_intel event wiring — event-driven-architecture.md §25.4.

Published: ``ioc_registered``, ``ioc_matched``. Consumed: ``evidence.ingested`` —
scan the new evidence against active IOCs and publish ``ioc_matched`` per hit. The
Inbox claim precedes any side effect; the business-level idempotency key
``(ioc_id, matched_evidence_id)`` prevents duplicate match rows (§25.4).
"""

from __future__ import annotations

from sentinelai.modules.threat_intel.repository import ThreatIntelUnitOfWork
from sentinelai.platform.events.dispatcher import EventDispatcher
from sentinelai.platform.events.envelope import EventEnvelope
from sentinelai.platform.events.inbox import InboxGuard

SCHEMA = "threat_intel"

# Published (§25.4).
EVENT_IOC_REGISTERED = "threat_intel.ioc_registered"
EVENT_IOC_MATCHED = "threat_intel.ioc_matched"

# Consumed (§25.4).
EVENT_EVIDENCE_INGESTED = "evidence.ingested"
_HANDLER_SCAN = "threat_intel.scan_for_ioc_matches"


async def on_evidence_ingested(event: EventEnvelope, uow: ThreatIntelUnitOfWork) -> None:
    """Scan newly ingested evidence against active IOCs; publish a match per hit."""
    guard = InboxGuard(uow.session, schema=SCHEMA)
    if not await guard.try_claim(event.event_id, handler_name=_HANDLER_SCAN):
        return
    raise NotImplementedError


def register_consumers(dispatcher: EventDispatcher) -> None:
    dispatcher.register(
        EVENT_EVIDENCE_INGESTED,
        on_evidence_ingested,
        inbox_schema=SCHEMA,
        uow_factory=ThreatIntelUnitOfWork,
    )
