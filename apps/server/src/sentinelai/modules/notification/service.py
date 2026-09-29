"""notification business logic (guide Part 5) — rule management, the caller's
notification inbox, and dispatch driven by consumed events.

Dispatch handlers enforce the catalog's tight business-idempotency keys (§25.9) so a replayed event
never re-sends a message the analyst already received.

**Two very different kinds of caller, and the difference shows in the constructor.** The four
``dispatch_for_*`` methods are driven by the event dispatcher, which hands a handler a session and a
signed outbox and nothing else — no principal, no KMS. The rule-management and redelivery methods
are driven by an authenticated admin over HTTP, and every one of them mutates who gets told what, so
they are audited. ``kms`` is therefore optional on construction and **required by `_audit`**: an
audited method reached without one fails loudly rather than performing the side effect unaudited
(security-architecture §22 — "no alternate route that produces an unaudited side effect").
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import UUID

from fastapi import Depends

from sentinelai.modules.notification.events import (
    EVENT_NOTIFICATION_DELIVERY_FAILED,
    EVENT_NOTIFICATION_DISPATCHED,
)
from sentinelai.modules.notification.exceptions import (
    NothingToRedeliverError,
    NotificationNotFoundError,
    NotificationRuleNotFoundError,
)
from sentinelai.modules.notification.models import (
    Notification,
    NotificationDelivery,
    NotificationRule,
)
from sentinelai.modules.notification.repository import NotificationUnitOfWork, get_notification_uow
from sentinelai.modules.notification.schemas import (
    NotificationRuleCreate,
    NotificationRuleUpdate,
)
from sentinelai.platform.auth.audit import record_audit_event
from sentinelai.platform.auth.dependencies import CurrentUser
from sentinelai.platform.crypto import get_kms
from sentinelai.platform.crypto.kms import KeyManagementService
from sentinelai.platform.logging import log
from sentinelai.platform.notifications import (
    NotificationMessage,
    NotificationSender,
    build_notification_sender,
)
from sentinelai.shared.exceptions import (
    ForbiddenError,
    PreconditionFailedError,
    ValidationFailedError,
)
from sentinelai.shared.pagination import PageParams, decode_cursor, encode_cursor

# The producing module recorded on each notification — half of the §25.9 idempotency key.
_MODULE_INGESTION = "ingestion"
_MODULE_INVESTIGATION = "investigation"
_MODULE_CASE_MANAGEMENT = "case_management"

_MODULE = "notification"

# Every event a rule may subscribe to: `event-driven-architecture.md` §25's complete **published**
# catalog, one entry per row of §25.1-§25.9's Published tables.
#
# **This resolves a documented conflict, and the resolution is not the obvious one.** api-design.md
# §8 says `trigger_event_type` "must be one of the event names catalogued in `system-design.md` §6".
# §6's table is explicitly labelled "names are illustrative of the convention ... not a final
# schema", it omits `case.report_generated` and `evidence.scanned` (two of the four events this
# module actually consumes), and it attributes `investigation.finding_reviewed` to
# `case-management`. `CLAUDE.md` makes `event-driven-architecture.md` authoritative for the event
# catalog, so §25 is the list and §6 is the prose pointing at it.
#
# Validated at all because the failure mode is silent: a rule naming an event nobody publishes never
# fires, and an admin who configured an alert would learn that only from the alert never arriving.
#
# A rule on `notification.dispatched` would be a feedback loop if the rule engine were ever wired —
# left in the set rather than excluded, because §25 catalogues it and excluding it would be a rule
# this document does not state. Whoever wires the engine owns that guard.
TRIGGER_EVENT_TYPES: frozenset[str] = frozenset(
    {
        # §25.1 platform
        "user.created",
        "user.disabled",
        "role.granted",
        "role.revoked",
        # §25.2 ingestion
        "evidence.ingested",
        "evidence.superseded",
        "evidence.validation_failed",
        "evidence.scanned",
        # §25.3 osint
        "osint.finding_captured",
        "osint.source_activated",
        "osint.source_deactivated",
        # §25.4 threat_intel
        "threat_intel.ioc_registered",
        "threat_intel.ioc_matched",
        # §25.5 forensics
        "forensics.artifact_registered",
        "forensics.artifact_processed",
        # §25.6 social_media
        "social_media.content_captured",
        "social_media.account_registered",
        # §25.7 case_management
        "case.created",
        "case.status_changed",
        "evidence.linked_to_case",
        "evidence.unlinked_from_case",
        "case.report_generated",
        # §25.8 investigation
        "investigation.correlation_run_completed",
        "investigation.correlation_run_failed",
        "investigation.correlation_generated",
        "investigation.finding_reviewed",
        # §25.9 notification (own)
        "notification.dispatched",
        "notification.delivery_failed",
    }
)

# Delivery statuses written to `notification_deliveries.delivery_status`. §3.6 types the column
# `varchar(30)` and names no vocabulary, so these three are this module's, kept in one place because
# `redeliver` has to recognise a failure the dispatch path wrote.
DELIVERY_DELIVERED = "delivered"
DELIVERY_FAILED = "failed"


def rule_etag(rule: NotificationRule) -> str:
    """A weak ETag over the rule's *mutable* fields (api-design.md §2.6).

    Only `name`, `channel` and `is_active` can change (`NotificationRuleUpdate`), so only those are
    in the digest. Including `trigger_event_type` — which a PATCH cannot alter — would make the ETag
    churn on nothing; including the id alone would make it never change and the `If-Match` guard
    decorative.
    """
    material = f"{rule.rule_id}|{rule.name}|{rule.channel}|{rule.is_active}"
    return f'W/"{hashlib.sha256(material.encode()).hexdigest()[:32]}"'


def _normalize_etag(value: str) -> str:
    """Compare ETags by their opaque value, ignoring the `W/` marker and quoting a client adds."""
    return value.strip().removeprefix("W/").strip('"')


class NotificationService:
    def __init__(
        self,
        uow: NotificationUnitOfWork,
        *,
        sender: NotificationSender | None = None,
        kms: KeyManagementService | None = None,
    ) -> None:
        self._uow = uow
        self._sender = sender if sender is not None else build_notification_sender()
        # Optional because the event dispatcher constructs this service with a session and nothing
        # else; `_audit` requires it, so an operator-driven method reached without one fails rather
        # than mutating unaudited.
        self._kms = kms

    async def _audit(
        self, actor: CurrentUser, action: str, target_id: UUID, details: dict[str, object]
    ) -> None:
        """Record an operator action on `platform.audit_log` (security-architecture §22).

        **Every method that calls this changes who gets told what.** A rule's `target_role_or_user`
        decides which analyst is alerted and `is_active` decides whether anyone is — so an insider
        who deactivates the rule that would have alerted a supervisor, or points it at themselves,
        is
        suppressing oversight. §22's threat model (a "malicious or coerced insider (investigator,
        admin)") is exactly that person, and the signed, hash-chained audit entry is what makes the
        action visible afterwards. `api-design.md` §8 says the rule endpoints "follow standard
        conventions"; auditing admin configuration is the convention `osint` and `threat_intel`
        already follow for their own config tables.

        Raises rather than skipping when no KMS was supplied. §22 requires that there be "no
        alternate route that produces an unaudited side effect" — so an unaudited route must not
        silently exist,
        and failing closed is the only way to guarantee that without threading a KMS into the event
        dispatcher that has no use for one.
        """
        if self._kms is None:
            raise RuntimeError(
                "notification audit requires a KMS: this service was constructed without one "
                "(the event-dispatcher path), and that path must not reach an audited method"
            )
        roles = actor.roles
        await record_audit_event(
            self._uow.session,
            kms=self._kms,
            actor_user_id=actor.user_id,
            actor_role=roles[0] if roles else "none",
            action=action,
            module=_MODULE,
            target_type="notification_rule",
            target_id=target_id,
            details=details,
        )

    async def list_notifications(
        self, actor: CurrentUser, page: PageParams, *, read: bool | None = None
    ) -> tuple[list[Notification], str | None, bool]:
        """The caller's OWN notifications, newest first (api-design.md §8).

        Returns ``(items, next_cursor, has_more)``. The recipient is always the authenticated
        actor — never a parameter — so one analyst's inbox is not reachable from another's
        session. The repository fetches ``limit + 1``; the extra row is the ``has_more`` signal
        and is trimmed off before it is returned.

        ``read`` is §8's documented boolean filter, and it is the filter an analyst actually works
        from: "what is waiting for me" is ``read=false``. It rides inside the cursor's own ordering
        rather than being applied afterwards, so paging a filtered list cannot skip rows.
        """
        cursor_created_at: datetime | None = None
        cursor_notification_id: UUID | None = None
        if page.cursor:
            raw_value, cursor_notification_id = decode_cursor(page.cursor)
            cursor_created_at = datetime.fromisoformat(raw_value)

        rows = await self._uow.notifications.list_for_recipient(
            actor.user_id,
            limit=page.limit,
            cursor_created_at=cursor_created_at,
            cursor_notification_id=cursor_notification_id,
            read=read,
        )
        has_more = len(rows) > page.limit
        items = list(rows[: page.limit])
        next_cursor = (
            encode_cursor(items[-1].created_at.isoformat(), items[-1].notification_id)
            if has_more and items
            else None
        )
        return items, next_cursor, has_more

    async def mark_read(self, notification_id: UUID, actor: CurrentUser) -> Notification:
        """Mark the caller's own notification read. Idempotent.

        A second call is a no-op that returns the notification unchanged — the original
        ``read_at`` is preserved, so re-reading never rewrites when the analyst first saw it.

        A notification belonging to someone else raises ``ForbiddenError`` (403), NOT 404 —
        api-design.md §8's deliberate, documented exception to the NOT_FOUND-hides-existence
        convention. Never commits: the entrypoint owns the transaction (ADR-0005).
        """
        notification = await self._uow.notifications.get_by_id(notification_id)
        if notification is None:
            raise NotificationNotFoundError()
        if notification.recipient_user_id != actor.user_id:
            raise ForbiddenError("notification belongs to another recipient")
        if notification.read_at is None:
            notification.read_at = datetime.now(UTC)
        return notification

    # -- operator surface (api-design.md §4.9/§8, admin-only, audited) ------
    async def redeliver(
        self, notification_id: UUID, actor: CurrentUser, correlation_id: str
    ) -> NotificationDelivery:
        """Retry a **failed** delivery for an existing notification (§4.9). Admin-only.

        **It re-sends; it never re-creates.** The notification row is the durable delivery in
        Phase 1,
        so a retry is a new row in `notification_deliveries` against the same notification — never a
        second notification. That distinction is what keeps this endpoint from becoming a way around
        §25.9's idempotency key: however many times an admin retries, the analyst's inbox holds one
        entry for the fact.

        Refuses with a 409 when the latest attempt succeeded. §4.9 defines the endpoint as retrying
        a
        *failed* delivery, and re-sending a delivered one is exactly the duplicate §25.9 exists to
        prevent. A notification with **no** attempts at all is retryable: that is what a delivery
        row lost to a rolled-back transaction looks like, and an analyst holding an undelivered
        notification is the case this endpoint is for.

        Synchronous despite the route's `202`. §4.9 documents no job for it and the sender is
        in-process, so `202` means "attempt made, outcome recorded in `notification_deliveries`"
        rather than "queued" — a client wanting the outcome reads the deliveries, exactly as it
        would after an async attempt. Never commits (ADR-0005).
        """
        notification = await self._uow.notifications.get_by_id(notification_id)
        if notification is None:
            raise NotificationNotFoundError()

        attempts = await self._uow.deliveries.list_for_notification(notification_id)
        if attempts and attempts[0].delivery_status == DELIVERY_DELIVERED:
            raise NothingToRedeliverError(
                f"notification {notification_id} was already delivered on "
                f"{attempts[0].channel}; there is no failed attempt to retry"
            )

        delivery = await self._attempt_delivery(
            notification,
            subject="Notification redelivery",
            body=notification.message or "",
            correlation_id=correlation_id,
        )
        await self._audit(
            actor,
            "notification.redelivered",
            notification_id,
            {
                "recipient_user_id": str(notification.recipient_user_id),
                "channel": delivery.channel,
                "delivery_status": delivery.delivery_status,
                "previous_attempts": len(attempts),
            },
        )
        return delivery

    async def list_rules(self, actor: CurrentUser) -> Sequence[NotificationRule]:
        """Every notification rule, active and inactive (§4.9). Admin-only, unpaginated.

        Not audited: §8 makes the personal notification list a non-audit surface and this is the
        same kind of read — configuration an admin is entitled to see. The *mutations* are audited,
        which is where the insider risk lives.
        """
        return await self._uow.rules.list_()

    async def create_rule(
        self, data: NotificationRuleCreate, actor: CurrentUser, correlation_id: str
    ) -> NotificationRule:
        """Create a notification rule (§4.9). New rules start active.

        ``trigger_event_type`` is validated against §25's published catalog
        (``TRIGGER_EVENT_TYPES``) because the failure mode is silent: a rule naming an event nobody
        publishes never fires, and the admin who configured the alert would discover that only from
        the alert never arriving. A 422 — the request is well-formed and the value fails a domain
        rule
        (§2.4's split).

        ``channel`` is **not** validated, deliberately. No document defines a channel vocabulary
        (§3.6 types it as free text) and only the logging adapter exists, so a check would either
        reject `email` — which an admin may legitimately pre-configure for an adapter that is coming
        — or invent a list. The same silent-dead-rule risk applies; it is recorded in the
        implementation log rather than guessed at here.

        Publishes nothing. §25.9's published table holds exactly two events, both about deliveries;
        there is no rule-lifecycle event, and inventing one would be inventing catalog.
        """
        if data.trigger_event_type not in TRIGGER_EVENT_TYPES:
            raise ValidationFailedError(
                [
                    {
                        "field": "trigger_event_type",
                        "message": (
                            "must be an event type published by some module "
                            "(event-driven-architecture.md §25); a rule on an unpublished event "
                            "would never fire"
                        ),
                    }
                ]
            )

        rule = NotificationRule(
            name=data.name,
            trigger_event_type=data.trigger_event_type,
            channel=data.channel,
            target_role_or_user=data.target_role_or_user,
            # §7 gives config data an `is_active` lifecycle rather than deletion, and a rule created
            # inactive would be configuration that silently does nothing.
            is_active=True,
        )
        await self._uow.rules.add(rule)
        await self._audit(
            actor,
            "notification_rule.created",
            rule.rule_id,
            {
                "name": data.name,
                "trigger_event_type": data.trigger_event_type,
                "channel": data.channel,
                # The field that decides who gets told — the one an insider would change.
                "target_role_or_user": str(data.target_role_or_user),
            },
        )
        return rule

    async def update_rule(
        self, rule_id: UUID, data: NotificationRuleUpdate, actor: CurrentUser, expected_etag: str
    ) -> NotificationRule:
        """Update or deactivate a rule (§4.9), ETag-guarded.

        ``If-Match`` is required by §2.6 for a mutable resource, and the guard runs before anything
        mutates: two admins editing one rule must not have one silently overwrite the other, which
        for a rule means one of them unknowingly re-enabling an alert the other just turned off.

        ``trigger_event_type`` and ``target_role_or_user`` are **not** updatable, matching
        `NotificationRuleUpdate`: §4.9 describes this endpoint as "Update/deactivate", and
        repointing an existing rule at a different event or a different recipient is a different
        rule — creating one leaves both in the audit trail, where editing in place would leave only
        the new state.

        Deactivation is audited with both the old and the new flag, because "who turned this alert
        off and when" is the question an oversight review asks.
        """
        rule = await self._uow.rules.get_by_id(rule_id)
        if rule is None:
            raise NotificationRuleNotFoundError()
        if _normalize_etag(expected_etag) != _normalize_etag(rule_etag(rule)):
            raise PreconditionFailedError("rule was modified concurrently (ETag mismatch)")

        changes: dict[str, object] = {}
        if data.name is not None and data.name != rule.name:
            changes["name"] = {"from": rule.name, "to": data.name}
            rule.name = data.name
        if data.channel is not None and data.channel != rule.channel:
            changes["channel"] = {"from": rule.channel, "to": data.channel}
            rule.channel = data.channel
        if data.is_active is not None and data.is_active != rule.is_active:
            changes["is_active"] = {"from": rule.is_active, "to": data.is_active}
            rule.is_active = data.is_active

        # Audited even when nothing changed: an admin who sent a PATCH with a valid `If-Match` acted
        # on this rule, and an oversight review asking "who touched the alerting configuration"
        # wants that, not only the diffs that happened to be non-empty.
        await self._audit(actor, "notification_rule.updated", rule_id, {"changes": changes})
        return rule

    # -- consumer-path dispatch (invoked from events.py handlers) -----------
    async def _create_and_dispatch(
        self,
        *,
        recipient_user_id: UUID,
        source_module: str,
        source_reference_id: UUID,
        subject: str,
        body: str,
        correlation_id: str,
        match_message: bool = False,
    ) -> Notification | None:
        """Shared dispatch core for every consumed-event handler.

        Applies the §25.9 business-idempotency check (``match_message=True`` adds the stored
        message to the key, for events like `case.status_changed` whose catalog key carries an
        extra discriminator), persists the notification, hands it to the sender, records the
        delivery outcome, and publishes the outcome event. Returns ``None`` when the analyst
        already has this message. Never commits (ADR-0005): the dispatcher owns the transaction.
        """
        if await self._uow.notifications.exists_for_source(
            recipient_user_id,
            source_module,
            source_reference_id,
            message=body if match_message else None,
        ):
            return None  # §25.9 idempotency key — the analyst already has this message

        notification = Notification(
            rule_id=None,  # system-generated: consumed-event dispatches are not rule-driven
            recipient_user_id=recipient_user_id,
            source_module=source_module,
            source_reference_id=source_reference_id,
            message=body,
            created_at=datetime.now(UTC),
            read_at=None,
        )
        await self._uow.notifications.add(notification)
        await self._attempt_delivery(
            notification, subject=subject, body=body, correlation_id=correlation_id
        )
        return notification

    async def _attempt_delivery(
        self,
        notification: Notification,
        *,
        subject: str,
        body: str,
        correlation_id: str,
    ) -> NotificationDelivery:
        """Hand one notification to the channel, record the outcome, announce it.

        Shared by first dispatch and by an admin's `redeliver`, which is the point: a retry must
        produce the same kind of delivery row and the same §25.9 outcome event as the original
        attempt,
        and two copies of this logic would eventually disagree about one of them.

        **A channel failure is recorded, never raised.** The in-app notification row is the durable
        delivery in Phase 1, so raising would roll back the whole handler transaction — the inbox
        claim, the notification, this row — and the retry would re-notify. The failure becomes a
        `failed` delivery row plus `notification.delivery_failed`, which is what an operator reads
        and what `redeliver` later acts on.
        """
        message = NotificationMessage(
            recipient_user_id=notification.recipient_user_id,
            subject=subject,
            body=body,
            channel=self._sender.channel,
        )
        try:
            await self._sender.send(message)
            status, delivered_at = DELIVERY_DELIVERED, datetime.now(UTC)
            event_type = EVENT_NOTIFICATION_DISPATCHED
            event_payload: dict[str, object] = {
                "notification_id": str(notification.notification_id),
                "channel": self._sender.channel,
            }
        except Exception as exc:
            log.warning(
                "notification_delivery_failed",
                channel=self._sender.channel,
                notification_id=str(notification.notification_id),
                error=type(exc).__name__,
            )
            status, delivered_at = DELIVERY_FAILED, None
            event_type = EVENT_NOTIFICATION_DELIVERY_FAILED
            event_payload = {
                "notification_id": str(notification.notification_id),
                "channel": self._sender.channel,
                "error": type(exc).__name__,
            }

        delivery = NotificationDelivery(
            notification_id=notification.notification_id,
            channel=self._sender.channel,
            delivery_status=status,
            attempted_at=datetime.now(UTC),
            delivered_at=delivered_at,
        )
        await self._uow.deliveries.add(delivery)
        await self._uow.outbox.publish(
            event_type=event_type,
            aggregate_type="notification",
            aggregate_id=notification.notification_id,
            payload=event_payload,
            correlation_id=correlation_id,
            actor_type="system",
        )
        return delivery

    async def dispatch_for_evidence_scanned(
        self,
        *,
        evidence_id: UUID,
        recipient_user_id: UUID,
        detection_name: str | None,
        engine: str,
        correlation_id: str,
    ) -> Notification | None:
        """Notify the uploading analyst that a detection blocked their upload (security §25).

        The caller has already decided this scan was a *block*. Idempotency key (§25.9):
        ``(recipient_user_id, source_module='ingestion', source_reference_id=evidence_id)``.
        """
        detection = detection_name or "an unnamed detection"
        return await self._create_and_dispatch(
            recipient_user_id=recipient_user_id,
            source_module=_MODULE_INGESTION,
            source_reference_id=evidence_id,
            subject="Evidence upload blocked by malware scan",
            body=(
                f"Upload blocked: malware detected in evidence {evidence_id} "
                f"({detection}, engine={engine}). The file is retained in quarantine and was "
                f"not promoted to the evidence store."
            ),
            correlation_id=correlation_id,
        )

    async def dispatch_for_correlation_generated(
        self,
        *,
        case_id: UUID,
        relationship_id: UUID,
        recipient_user_id: UUID,
        correlation_id: str,
    ) -> Notification | None:
        """Tell the case's investigator that the AI proposed a new relationship to review.

        Idempotency key (§25.9): ``(recipient_user_id, source_module='investigation',
        source_reference_id=relationship_id)`` — the catalog calls this the tightest key it has,
        because a replayed event must never re-send a review request already delivered.

        The finding stays ``proposed`` until a human reviews it (PRD FR-7.3); this notification
        is the prompt to do so, never an approval.
        """
        return await self._create_and_dispatch(
            recipient_user_id=recipient_user_id,
            source_module=_MODULE_INVESTIGATION,
            source_reference_id=relationship_id,
            subject="New AI-proposed finding awaiting review",
            body=(
                f"A new relationship ({relationship_id}) was proposed for case {case_id} and is "
                f"awaiting your review. It remains proposed until you confirm or reject it."
            ),
            correlation_id=correlation_id,
        )

    async def dispatch_for_case_status_changed(
        self,
        *,
        case_id: UUID,
        new_status: str,
        recipient_user_id: UUID,
        correlation_id: str,
    ) -> Notification | None:
        """Notify the case's investigator of a lifecycle transition.

        Idempotency key (§25.9): ``(recipient_user_id, source_reference_id=case_id, new_status)``
        — the ``new_status`` discriminator is carried by matching the stored message, which is
        composed as a pure function of exactly ``case_id`` and ``new_status`` (no timestamp, no
        previous status), so the comparison is stable. Reaching the same status twice therefore
        notifies once, which is what that key specifies.
        """
        return await self._create_and_dispatch(
            recipient_user_id=recipient_user_id,
            source_module=_MODULE_CASE_MANAGEMENT,
            source_reference_id=case_id,
            subject=f"Case {case_id} is now {new_status}",
            body=f"Case {case_id} status changed to {new_status}.",
            correlation_id=correlation_id,
            match_message=True,  # the key carries new_status beyond (recipient, case)
        )

    async def dispatch_for_report_generated(
        self,
        *,
        case_id: UUID,
        report_id: UUID,
        recipient_user_id: UUID,
        correlation_id: str,
    ) -> Notification | None:
        """Tell the requester their case report is ready to download.

        Idempotency key (§25.9): ``(recipient_user_id, source_reference_id=report_id)`` — scoped
        to the report, so regenerating a case's report is a genuinely new fact and does notify.
        """
        return await self._create_and_dispatch(
            recipient_user_id=recipient_user_id,
            source_module=_MODULE_CASE_MANAGEMENT,
            source_reference_id=report_id,
            subject="Case report ready for download",
            body=(
                f"The report ({report_id}) you requested for case {case_id} has finished "
                f"generating and is ready to download."
            ),
            correlation_id=correlation_id,
        )


def get_notification_service(
    uow: NotificationUnitOfWork = Depends(get_notification_uow),
    kms: KeyManagementService = Depends(get_kms),
) -> NotificationService:
    """The HTTP-path service. The KMS is **required here** and absent on the dispatcher path.

    Every operator-driven method on this service is audited, and ``_audit`` needs the KMS to sign
    the entry (ADR-0003 §1). The event dispatcher builds the service with a session only, and the
    methods it reaches do not audit — so making it optional on the class and mandatory in this
    provider is what keeps the audited routes audited without threading a KMS into a handler that
    has no use for one.
    """
    return NotificationService(uow, kms=kms)
