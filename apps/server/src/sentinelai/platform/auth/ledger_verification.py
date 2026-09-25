"""Reading the evidentiary ledgers back for verification — ADR-0003 §6, Wave 1.4.

Persistence and orchestration for :mod:`sentinelai.platform.crypto.verification`, which is pure and
deliberately touches no database. This module supplies it with rows:

* ``platform.ledger_anchors`` reads, used by **both** ledgers. Anchoring is generic over what it
  commits to (one table, discriminated by a ``ledger`` column — see
  :class:`~sentinelai.platform.auth.models.LedgerAnchor`), so the custody chain's anchors are read
  through here too, by ``modules.ingestion``. That direction is legal and intended: ``ingestion``
  may use ``platform``; the reverse would not be.
* ``platform.audit_log`` reads and the audit chain's own verification service. The audit ledger is
  platform's own table, so unlike the custody ledger there is no module that could own this.

**Why the audit ledger is verified in two different scopes.** The custody chain for one evidence
item is small and bounded, so its report covers every entry. The audit ledger is global,
append-only, and grows forever — re-verifying every row on a schedule is not viable, and
pretending otherwise would produce a job that works in testing and times out in production. So:

* **Entry-level checks** (link continuity, hash recompute, signature) run over a bounded window of
  the most recent entries. This is where tampering with a recent action would show up.
* **Anchor checks** run against the ledger's *complete* ordered list of entry hashes, which is one
  indexed text column and cheap to stream. This scope is not negotiable: an anchor exists precisely
  to catch a deleted tail, and a deleted tail is by definition not in a window of surviving rows.
  Checking anchors against the window would report every older anchor as a missing range while
  silently failing to notice the one thing anchors are for.

That split is the whole reason ``verify_chain`` takes ``chain_entry_hashes`` separately from
``entries``.
"""

from __future__ import annotations

import base64
from collections.abc import Sequence
from datetime import datetime
from typing import Final

from cryptography import x509
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sentinelai.platform.auth.audit import audit_preimage_fields
from sentinelai.platform.auth.models import AuditLog, LedgerAnchor
from sentinelai.platform.crypto.ledger import LEDGER_AUDIT, LedgerSigner
from sentinelai.platform.crypto.verification import (
    AnchorView,
    LedgerEntryView,
    LedgerVerificationReport,
    LedgerVerifier,
)

# Default window for the audit ledger's entry-level checks. Large enough that a tampered recent
# action is caught long before the next run, small enough to stay well inside the worker's
# `job_timeout`. Overridable per call; the anchor layer is unaffected by it either way.
DEFAULT_AUDIT_ENTRY_WINDOW: Final = 5_000


async def read_anchor_views(session: AsyncSession, ledger: str) -> list[AnchorView]:
    """Every anchor recorded for ``ledger``, oldest first.

    Ordered by ``created_at`` so a report lists anchors in the order they were published, which is
    the order an auditor reconstructing a timeline expects. The ordering has no bearing on the
    verdict — each anchor is checked independently against the chain.
    """
    rows = (
        (
            await session.execute(
                select(LedgerAnchor)
                .where(LedgerAnchor.ledger == ledger)
                .order_by(LedgerAnchor.created_at)
            )
        )
        .scalars()
        .all()
    )
    return [
        AnchorView(
            anchor_id=row.anchor_id,
            ledger=row.ledger,
            merkle_root=row.merkle_root,
            first_entry_hash=row.first_entry_hash,
            last_entry_hash=row.last_entry_hash,
            entry_count=row.entry_count,
            signature_envelope=row.signature,
            worm_object_ref=row.worm_object_ref,
            # Wave 1.3c. The column holds the base64 of the DER token, not a pointer to it: the
            # token is a few hundred bytes, and a reference to something stored elsewhere would be
            # one more thing that can go missing between an anchor and its proof of time. Base64
            # because the column is `Text` (ADR-0003 §5) and changing an evidentiary table's column
            # type is a migration this does not need.
            tsa_token=base64.b64decode(row.tsa_token_ref) if row.tsa_token_ref else None,
        )
        for row in rows
    ]


def audit_entry_view(row: AuditLog) -> LedgerEntryView:
    """Rebuild one ``platform.audit_log`` row's verification view.

    ``preimage_fields`` is reconstructed with :func:`audit_preimage_fields` — the same function the
    writer used — but only when the row claims a preimage version. A ``NULL`` there marks a
    pre-Wave-1.2 row whose hash covered a different, incomplete field set; handing the engine a
    freshly-built complete preimage for it would produce a mismatch and report honest legacy
    history as forged. ``None`` is the truthful input, and the engine turns it into ``partial``.
    """
    fields = (
        audit_preimage_fields(
            prev_hash=row.prev_entry_hash,
            audit_id=row.audit_id,
            occurred_at=row.occurred_at,
            actor_user_id=row.actor_user_id,
            actor_role=row.actor_role,
            action=row.action,
            module=row.module,
            target_type=row.target_type,
            target_id=row.target_id,
            ip_address=row.ip_address,
            user_agent=row.user_agent,
            details=row.details,
        )
        if row.preimage_version is not None
        else None
    )
    return LedgerEntryView(
        # `audit_log` has no sequence column — its ordering is the chain itself, and the signed
        # message carries `None` for it (see `ledger.signed_message`). Passing anything else here
        # would compute a different message and fail every signature.
        sequence=None,
        entry_hash=row.entry_hash,
        prev_hash=row.prev_entry_hash,
        hash_algo=row.hash_algo,
        preimage_version=row.preimage_version,
        preimage_fields=fields,
        signature_envelope=row.signature,
    )


async def read_audit_chain_hashes(
    session: AsyncSession, *, before: datetime | None = None
) -> list[str]:
    """Every audit entry hash in chain order — the anchor layer's input.

    One text column, so this stays cheap even on a large ledger. Ordered by ``occurred_at``, which
    is how the writer picks the head it chains onto
    (:func:`sentinelai.platform.auth.audit.record_audit_event`), so it reproduces write order.

    ``audit_id`` is the tiebreak, and it is load-bearing rather than cosmetic: ``occurred_at``
    alone is not a total order, so two processes could otherwise derive different leaf orders and
    therefore different Merkle roots for the same ledger. With the tiebreak the order is total and
    reproducible.

    ``before`` excludes entries at or after a watermark. The anchor cutter uses it so a write whose
    clock ran slightly behind cannot land *inside* an already-anchored range and break its
    contiguity.

    The ordering is still a heuristic in one respect, exactly as it is on the write path: clocks are
    not monotonic. It cannot mask tampering — a reordered pair changes the recomputed Merkle root
    either way — but a clock inversion wider than the cutter's watermark could surface as an anchor
    mismatch on an intact ledger. The chain-link uniqueness index from Wave 1.3 makes that the only
    remaining ordering ambiguity, and closing it properly needs a monotonic sequence column on
    ``audit_log``, which is a schema change and therefore its own increment.
    """
    statement = select(AuditLog.entry_hash).order_by(AuditLog.occurred_at, AuditLog.audit_id)
    if before is not None:
        statement = statement.where(AuditLog.occurred_at < before)
    return list((await session.execute(statement)).scalars().all())


async def read_recent_audit_entries(
    session: AsyncSession, *, limit: int = DEFAULT_AUDIT_ENTRY_WINDOW
) -> list[LedgerEntryView]:
    """The most recent ``limit`` audit entries, returned oldest-first for chain walking.

    Fetched newest-first (so the window is the *recent* end of the ledger) and then reversed,
    because the verifier walks forward and compares each entry's ``prev_hash`` against its
    predecessor.
    """
    rows = (
        (await session.execute(select(AuditLog).order_by(AuditLog.occurred_at.desc()).limit(limit)))
        .scalars()
        .all()
    )
    return [audit_entry_view(row) for row in reversed(rows)]


class AuditLedgerVerificationService:
    """Verifies ``platform.audit_log`` — ADR-0003 §6.

    Read-only. It issues ``SELECT``s and KMS *verify* calls (which use no private key), so it is
    safe to run from the HTTP process and from the worker, and it can never alter the ledger it is
    judging.
    """

    def __init__(
        self,
        session: AsyncSession,
        signer: LedgerSigner,
        *,
        tsa_trust_anchors: Sequence[x509.Certificate] = (),
    ) -> None:
        self._session = session
        # Empty by default: a deployment with no TSA configured has no anchors, and every anchor cut
        # before Wave 1.3c carries no token either. A token present with an empty store is refused
        # by the engine rather than skipped — see `LedgerVerifier.__init__`.
        self._verifier = LedgerVerifier(signer, tsa_trust_anchors=tsa_trust_anchors)

    async def verify(
        self, *, entry_window: int = DEFAULT_AUDIT_ENTRY_WINDOW
    ) -> LedgerVerificationReport:
        """Verify the recent entry window in full, and every anchor against the whole chain."""
        entries = await read_recent_audit_entries(self._session, limit=entry_window)
        return await self._verifier.verify_chain(
            ledger=LEDGER_AUDIT,
            entries=entries,
            anchors=await read_anchor_views(self._session, LEDGER_AUDIT),
            chain_entry_hashes=await read_audit_chain_hashes(self._session),
            # A window into a global, unbounded ledger does not begin at genesis. Demanding the
            # sentinel here would report every run after the first `entry_window` entries as a
            # missing head.
            expect_genesis=False,
        )


def summarize_findings(report: LedgerVerificationReport) -> dict[str, int]:
    """Finding-type -> count, for a log line or a metric fan-out.

    Entry and anchor findings are counted into one mapping deliberately: an operator reading an
    alarm wants to know *what is wrong with this ledger*, and splitting the counts across two keys
    by which layer noticed is a distinction only this module's authors care about.
    """
    counts: dict[str, int] = {}
    sources: Sequence[Sequence[object]] = [
        [f for e in report.entries for f in e.findings],
        [f for a in report.anchors for f in a.findings],
    ]
    for group in sources:
        for finding in group:
            key = str(finding)
            counts[key] = counts.get(key, 0) + 1
    return counts


__all__ = [
    "DEFAULT_AUDIT_ENTRY_WINDOW",
    "AuditLedgerVerificationService",
    "audit_entry_view",
    "read_anchor_views",
    "read_audit_chain_hashes",
    "read_recent_audit_entries",
    "summarize_findings",
]
