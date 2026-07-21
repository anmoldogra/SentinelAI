"""case_management event wiring — event-driven-architecture.md §25.7.

Published events are emitted from ``service.py`` via ``uow.outbox.publish`` (the
constants below are the catalog names). Consumed events are handled here: every
handler performs the Inbox claim before any side effect (§17), then acts in the
module's own vocabulary. ``register_consumers`` is called by the HTTP composition
root against the in-process dispatcher.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sentinelai.modules.case_management.models import CaseStatusHistory
from sentinelai.modules.case_management.repository import CaseManagementUnitOfWork
from sentinelai.platform.events.dispatcher import EventDispatcher
from sentinelai.platform.events.envelope import EventEnvelope
from sentinelai.platform.events.inbox import InboxGuard

SCHEMA = "case_management"

# Published (§25.7).
EVENT_CASE_CREATED = "case.created"
EVENT_CASE_STATUS_CHANGED = "case.status_changed"
EVENT_EVIDENCE_LINKED_TO_CASE = "evidence.linked_to_case"
EVENT_EVIDENCE_UNLINKED_FROM_CASE = "evidence.unlinked_from_case"
EVENT_CASE_REPORT_GENERATED = "case.report_generated"

# Consumed (§25.7).
EVENT_INVESTIGATION_FINDING_REVIEWED = "investigation.finding_reviewed"
_HANDLER_FINDING_REVIEWED = "case_management.on_investigation_finding_reviewed"


async def on_investigation_finding_reviewed(
    event: EventEnvelope, uow: CaseManagementUnitOfWork
) -> None:
    """Append a ``case_status_history`` row summarizing the disposition — in
    case_management's OWN vocabulary, no stored pointer into investigation (§25.7,
    database-design §5 acyclicity)."""
    guard = InboxGuard(uow.session, schema=SCHEMA)
    if not await guard.try_claim(event.event_id, handler_name=_HANDLER_FINDING_REVIEWED):
        return

    payload = event.payload
    case_id = UUID(str(payload["case_id"]))
    case = await uow.cases.get_by_id(case_id)
    if case is not None:
        disposition = str(payload.get("disposition", "reviewed"))
        relationship_id = payload.get("relationship_id")
        reviewed_by = payload.get("reviewed_by")
        actor_user_id = UUID(str(reviewed_by)) if reviewed_by else case.owning_user_id
        # Record the FACT of the review in case_management's own vocabulary — a
        # status-history note, no stored pointer into investigation (§25.7). Status
        # itself is unchanged (previous == new).
        await uow.status_history.add(
            CaseStatusHistory(
                case_id=case_id,
                previous_status=case.status,
                new_status=case.status,
                actor_user_id=actor_user_id,
                changed_at=datetime.now(UTC),
                notes=f"investigation finding {relationship_id} reviewed: {disposition}",
            )
        )
    await guard.mark_processed(event.event_id, handler_name=_HANDLER_FINDING_REVIEWED)


def register_consumers(dispatcher: EventDispatcher) -> None:
    """Register this module's event handlers with the in-process dispatcher."""
    dispatcher.register(
        EVENT_INVESTIGATION_FINDING_REVIEWED,
        on_investigation_finding_reviewed,
        inbox_schema=SCHEMA,
        uow_factory=CaseManagementUnitOfWork,
    )
