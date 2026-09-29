"""threat_intel event wiring — event-driven-architecture.md §25.4.

Published: ``ioc_registered``, ``ioc_matched``. Consumed: ``evidence.ingested`` —
scan the new evidence against active IOCs and publish ``ioc_matched`` per hit. The
Inbox claim precedes any side effect; the business-level idempotency key
``(ioc_id, matched_evidence_id)`` prevents duplicate match rows (§25.4).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sentinelai.modules.ingestion.public import read_evidence_attributes
from sentinelai.modules.threat_intel.matching import candidate_tokens
from sentinelai.modules.threat_intel.models import IocEvidenceMatch
from sentinelai.modules.threat_intel.repository import ThreatIntelUnitOfWork
from sentinelai.platform.events.dispatcher import EventDispatcher
from sentinelai.platform.events.envelope import EventEnvelope
from sentinelai.platform.events.inbox import InboxGuard
from sentinelai.platform.logging import log

SCHEMA = "threat_intel"

# Published (§25.4).
EVENT_IOC_REGISTERED = "threat_intel.ioc_registered"
EVENT_IOC_MATCHED = "threat_intel.ioc_matched"

# Consumed (§25.4).
EVENT_EVIDENCE_INGESTED = "evidence.ingested"
_HANDLER_SCAN = "threat_intel.scan_for_ioc_matches"

# A match found by exact comparison of a normalized indicator against a normalized evidence
# token is not a heuristic — the indicator is either present or it is not. The confidence a
# match carries is therefore the certainty of the *observation*, not a similarity score, and
# `matching.py` produces no fuzzy hits that would deserve a lower one. Named rather than
# inlined because §25.4's payload carries it, and a future fuzzy matcher would need a second
# value: the place to add one is here, beside the reason this is 1.
MATCH_CONFIDENCE = Decimal("1.000")


async def on_evidence_ingested(event: EventEnvelope, uow: ThreatIntelUnitOfWork) -> None:
    """Scan newly ingested evidence against active IOCs; publish a match per hit (§25.4).

    Two layers of idempotency, and both are load-bearing. The **inbox claim** stops the handler
    re-running on redelivery; the **`(ioc_id, matched_evidence_id)` pair check** inside the service
    stops a duplicate match row even when the claim is bypassed — which event-driven §Replay
    describes as a normal operation, since replaying `evidence.ingested` after a matcher fix is
    exactly how an operator would want to re-scan history.

    The matcher is a module-level function, not a service method, because the dispatcher hands a
    handler a session and a signed outbox and nothing else. It reads the evidence's `attributes`
    through `ingestion.public` — the §181 fetch path for what the event deliberately omits.
    """
    guard = InboxGuard(uow.session, schema=SCHEMA)
    if not await guard.try_claim(event.event_id, handler_name=_HANDLER_SCAN):
        return

    evidence_id = _uuid(event.payload.get("evidence_id"))
    if evidence_id is None:
        # Malformed payload: mark it handled rather than dead-lettering. The fact happened on
        # ingestion's side, a missing id is not recoverable by retry, and a raising handler would
        # block this aggregate's whole queue under ADR-0006's per-aggregate ordering.
        log.info("ioc_scan_skipped", reason="no evidence_id in payload")
        await guard.mark_processed(event.event_id, handler_name=_HANDLER_SCAN)
        return

    await scan_evidence_for_matches(
        uow,
        evidence_id=evidence_id,
        category=str(event.payload.get("category", "")),
        correlation_id=str(event.correlation_id),
    )
    await guard.mark_processed(event.event_id, handler_name=_HANDLER_SCAN)


def _uuid(value: object) -> UUID | None:
    if not isinstance(value, str):
        return None
    try:
        return UUID(value)
    except ValueError:
        return None


async def scan_evidence_for_matches(
    uow: ThreatIntelUnitOfWork,
    *,
    evidence_id: UUID,
    category: str,
    correlation_id: str,
    read_attributes: Callable[[UUID], Awaitable[dict[str, Any] | None]] | None = None,
) -> int:
    """Scan one evidence object against active IOCs; publish a match per hit. Returns the count.

    §25.4's handler action, and the reason `threat_intel` consumes `evidence.ingested` at all.

    **A function, not a service method.** The dispatcher hands a handler a session and a signed
    outbox and nothing else — no KMS, no object storage — so anything on the consumer path must run
    on exactly that. `ThreatIntelService` requires a KMS because every method on it is an audited
    user action; a match is neither, so forcing it through the class would have meant constructing
    one with nulls for dependencies this path never touches.

    A match is **not audited**, deliberately: `platform.audit_log` records what principals did, and
    no principal did this. The record of the observation is the match row plus the `ioc_matched`
    event — both durable, both attributable to the platform rather than to whoever happened to
    upload the evidence.

    `(ioc_id, matched_evidence_id)` is the idempotency key §25.4 names: "never create a duplicate
    match row for the same pair". Checked before inserting **and** enforced by a unique index — the
    check keeps redelivery quiet, the constraint makes two concurrent scans impossible rather than
    merely unlikely (the belt-and-suspenders ADR-0011 §4 asks for).

    Evidence whose attributes cannot be read is skipped, not failed: the row may have been removed
    between the event and this scan, and a handler that raised would dead-letter an event describing
    something that genuinely happened, then block its aggregate's queue under ADR-0006's
    per-aggregate ordering.

    `read_attributes` is injectable so a test can drive the matcher without an `ingestion` schema;
    the default is the real §181 fetch path.
    """
    reader = read_attributes or (lambda eid: read_evidence_attributes(uow.session, eid))
    attributes = await reader(evidence_id)
    if attributes is None:
        log.info("ioc_scan_skipped", reason="evidence not found", evidence_id=str(evidence_id))
        return 0

    tokens = candidate_tokens(attributes)
    if not tokens:
        return 0
    hits = await uow.iocs.find_by_values(sorted(tokens))

    matched = 0
    now = datetime.now(UTC)
    for ioc in hits:
        if await uow.matches.exists_for_pair(ioc_id=ioc.ioc_id, matched_evidence_id=evidence_id):
            continue
        await uow.matches.add(
            IocEvidenceMatch(
                ioc_id=ioc.ioc_id,
                matched_evidence_id=evidence_id,
                matched_at=now,
                confidence=MATCH_CONFIDENCE,
            )
        )
        # The indicator has just been seen in live evidence, which is what `last_seen` records — and
        # it is the field an analyst uses to tell a current threat from a stale one.
        ioc.last_seen = now
        await uow.outbox.publish(
            event_type=EVENT_IOC_MATCHED,
            aggregate_type="ioc",
            aggregate_id=ioc.ioc_id,
            payload={
                "ioc_id": str(ioc.ioc_id),
                "matched_evidence_id": str(evidence_id),
                "confidence": str(MATCH_CONFIDENCE),
            },
            correlation_id=correlation_id,
            # No `actor_ref`: a match is the platform's own observation, not a user's action.
            # Attributing it to whoever uploaded the evidence would misreport who decided it.
            actor_type="system",
        )
        matched += 1

    if matched:
        log.info(
            "ioc_matches_recorded",
            evidence_id=str(evidence_id),
            category=category,
            matches=matched,
        )
    return matched


def register_consumers(dispatcher: EventDispatcher) -> None:
    dispatcher.register(
        EVENT_EVIDENCE_INGESTED,
        on_evidence_ingested,
        inbox_schema=SCHEMA,
        uow_factory=ThreatIntelUnitOfWork,
    )
