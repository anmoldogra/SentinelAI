"""Administrative HTTP routes — api-design.md §4.1 and §10.

Currently one endpoint: ``GET /api/v1/admin/audit-log``, which implements PRD FR-9.3 (audit log
export). The rest of §4.1's admin group (`/admin/users`, `/admin/roles`) is still unbuilt;
provisioning happens through ``sentinelai.cli.admin`` for now.

**Why this lives in ``platform`` and not a module.** ``platform.audit_log`` is platform-owned —
every module writes to it through ``record_audit_event`` and none owns it — so an admin router in,
say, ``case_management`` would be a module reaching across a schema boundary for a table that is
not its own. The import DAG is satisfied either way; only this placement matches who owns the data.

**Read-only, and that is structural.** §10: "There is no `DELETE` anywhere in this endpoint group —
the audit log has no API-level erasure path at all." The repository behind this router exposes no
write method, so there is nothing for a future handler to call even by mistake, and ADR-0004's
trigger would refuse it regardless.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from sentinelai.platform.admin.repository import AuditLogFilters, AuditLogRepository
from sentinelai.platform.admin.schemas import AuditLogEntryRead
from sentinelai.platform.auth.dependencies import CurrentUser, require_role
from sentinelai.platform.auth.models import AuditLog
from sentinelai.platform.db.session import get_session
from sentinelai.platform.db.transaction import TransactionalRoute, bind_session
from sentinelai.shared.envelope import ListEnvelope, Meta, Pagination
from sentinelai.shared.pagination import PageParams, decode_cursor, encode_cursor, page_params

router = APIRouter(
    prefix="/api/v1",
    tags=["admin"],
    # ADR-0005 §1: the entrypoint owns the transaction. This router only reads, so the commit is a
    # no-op — declared anyway, because a router whose transaction handling differs from every other
    # router's is a trap for whoever adds the first mutating admin endpoint.
    route_class=TransactionalRoute,
    dependencies=[Depends(bind_session)],
)

# api-design.md §4.1 gives this endpoint `admin, compliance`. `compliance` is the point: an audit
# export is what an oversight body reads, and requiring `admin` for it would mean the people
# reviewing the operators had to be operators.
_AUDIT_ROLES = ("admin", "compliance")


def _meta(request: Request) -> Meta:
    return Meta(request_id=request.state.request_id, correlation_id=request.state.correlation_id)


def _to_read(entry: AuditLog) -> AuditLogEntryRead:
    """Map one entry, base64-encoding the signature so it survives JSON.

    ``model_validate`` cannot do this alone: ``signature`` is ``bytes`` on the model and JSON has no
    byte string, so it is encoded explicitly rather than left to a serializer default that might
    change shape between library versions — a reviewer's verification script depends on this being
    exactly base64.
    """
    import base64

    return AuditLogEntryRead(
        audit_id=entry.audit_id,
        occurred_at=entry.occurred_at,
        actor_user_id=entry.actor_user_id,
        actor_role=entry.actor_role,
        action=entry.action,
        module=entry.module,
        target_type=entry.target_type,
        target_id=entry.target_id,
        ip_address=entry.ip_address,
        user_agent=entry.user_agent,
        details=entry.details,
        prev_entry_hash=entry.prev_entry_hash,
        entry_hash=entry.entry_hash,
        hash_algo=entry.hash_algo,
        sig_alg=entry.sig_alg,
        key_id=entry.key_id,
        preimage_version=entry.preimage_version,
        signature=(
            base64.b64encode(entry.signature).decode("ascii")
            if entry.signature is not None
            else None
        ),
    )


@router.get("/admin/audit-log", response_model=ListEnvelope[AuditLogEntryRead])
async def list_audit_log(
    request: Request,
    actor_user_id: UUID | None = Query(default=None),
    action: str | None = Query(default=None),
    target_type: str | None = Query(default=None),
    target_id: UUID | None = Query(default=None),
    occurred_after: datetime | None = Query(default=None),
    occurred_before: datetime | None = Query(default=None),
    page: PageParams = Depends(page_params),
    current_user: CurrentUser = Depends(require_role(*_AUDIT_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> ListEnvelope[AuditLogEntryRead]:
    """Query and export the system audit log (api-design.md §10, PRD FR-9.3).

    Entries come back in **chain order** — ascending ``(occurred_at, audit_id)`` — because the
    export is meant to be verified, and verifying `entry_hash` links requires the order they were
    written in. Newest-first would be friendlier to a UI and useless to a reviewer.

    Each entry carries ``prev_entry_hash`` and ``entry_hash`` so that chain can be recomputed
    independently, plus the signature and the agility fields naming the algorithm and key to verify
    it under. That combination is what makes this an export of evidence rather than a report about
    it: recomputing the hashes proves the entries are self-consistent, and only the signatures prove
    an insider did not rewrite the whole chain (ADR-0003 §1).

    Reading the audit log is not itself audited. That is deliberate and worth stating: an audit
    entry per audit read would grow the table on every page of an export and, because each entry
    chains to the last, would interleave the reader's own footprints into the chain being exported.
    The access is authorized and logged at the request level; §10 asks for neither more nor less.
    """
    filters = AuditLogFilters(
        actor_user_id=actor_user_id,
        action=action,
        target_type=target_type,
        target_id=target_id,
        occurred_after=occurred_after,
        occurred_before=occurred_before,
    )
    after = None
    if page.cursor is not None:
        sort_value, last_id = decode_cursor(page.cursor)
        after = (datetime.fromisoformat(sort_value), last_id)

    # One extra row, to answer `has_more` without a second COUNT over a table that only grows.
    rows = await AuditLogRepository(session).page(filters, limit=page.limit + 1, after=after)
    has_more = len(rows) > page.limit
    entries = list(rows[: page.limit])

    next_cursor = (
        encode_cursor(entries[-1].occurred_at.isoformat(), entries[-1].audit_id)
        if has_more and entries
        else None
    )
    return ListEnvelope(
        data=[_to_read(entry) for entry in entries],
        pagination=Pagination(next_cursor=next_cursor, has_more=has_more, limit=page.limit),
        meta=_meta(request),
    )


__all__ = ["router"]
