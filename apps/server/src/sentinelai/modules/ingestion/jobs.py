"""ingestion background jobs — arq (guide Part 9 & 12).

Malware scanning runs as a background job after upload confirmation; promotion from the
``quarantine`` bucket to the evidence bucket happens only after a scan (security §24-25's
category-aware policy is enforced in the service, not here). The job is a thin composition root:
it resolves the session/storage/scanner from the worker context and delegates — all business
logic lives in ``EvidenceService`` (guide Part 5).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sentinelai.modules.ingestion.repository import IngestionUnitOfWork
from sentinelai.modules.ingestion.service import EvidenceService
from sentinelai.platform.logging import log
from sentinelai.platform.security.scanner import build_malware_scanner
from sentinelai.platform.storage import build_object_storage


async def scan_uploaded_evidence(ctx: dict[str, Any], evidence_id: UUID) -> None:
    """Scan a freshly-uploaded object; promote it out of quarantine per §25 policy.

    Retry-safe: arq retries this job (``WorkerSettings.max_tries``) and
    ``EvidenceService.scan_and_promote`` is idempotent, so a redelivery after a partial failure
    neither double-promotes nor loses the promotion record.
    """
    session_factory = ctx["session_factory"]
    scanner = ctx.get("malware_scanner") or build_malware_scanner()
    storage = ctx.get("object_storage") or build_object_storage()

    async with session_factory() as session:
        uow = IngestionUnitOfWork(session)
        service = EvidenceService(uow, storage=storage)
        try:
            await service.scan_and_promote(evidence_id, scanner, correlation_id=ctx.get("job_id"))
            await uow.commit()  # ADR-0005: the entrypoint (this job wrapper) owns the transaction
        except Exception:
            await uow.rollback()
            # No scan verdict is recorded on failure — a scan that could not run must never be
            # mistaken for a clean one. arq retries; a permanently failing job dead-letters.
            log.warning("evidence_scan_failed", evidence_id=str(evidence_id))
            raise
