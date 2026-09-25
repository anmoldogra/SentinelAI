"""Scheduled anchor batch cutting — ADR-0003 §3, the half IC-029 left unbuilt.

IC-029 built anchoring as "a library plus a store, not a running job": `LedgerAnchorService`
could publish an anchor and `platform.ledger_anchors` could record one, but nothing ever called
them. IC-030 then built a verification engine that checks whatever anchors exist — which, in a
deployment where no batch is ever cut, is none. The truncation defence was therefore fully
implemented and completely unarmed: every piece worked, and together they proved nothing.

This job is what arms it.

**What one run does.** For each evidentiary ledger: read the chain's entry hashes in their
canonical global order, find the contiguous tail that no existing anchor covers, and publish one
anchor over it — WORM object first, database row second.

**Why the tail and not "everything unanchored".** An anchor commits to a *contiguous range* of
one ordered list. Anchoring a set of scattered gaps would produce a root that no verifier could
reconcile against the chain, because `_slice_between` locates a range by its endpoints and expects
everything between them to belong to it. Cutting strictly forward from the last anchored position
keeps every anchor a clean, verifiable interval.

**The watermark, and why it is not optional.** Entries are ordered by `occurred_at`, and clocks
are not monotonic. A write whose clock ran a few seconds behind can be committed *after* a batch
is cut but sort *before* its boundary — landing inside an already-anchored range and changing the
recomputed root for a ledger nobody touched. That is a false tampering alarm on healthy data,
which is the failure mode most likely to get a real alarm ignored. So the cutter never anchors
anything newer than `ANCHOR_WATERMARK_MINUTES`, trading a bounded delay in coverage for immunity
to ordinary clock skew.

**Ordering within a run.** Custody is cut before audit, deliberately. Both are safe to interrupt —
each ledger's cut is independent and idempotent — but custody is the chain a court actually asks
about, so it wins the KMS round-trip if the run is killed midway.
"""

from __future__ import annotations

import base64
import time
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from sentinelai.modules.ingestion.repository import IngestionUnitOfWork
from sentinelai.platform.auth.ledger_verification import (
    read_anchor_views,
    read_audit_chain_hashes,
)
from sentinelai.platform.auth.models import LedgerAnchor
from sentinelai.platform.config import Settings
from sentinelai.platform.config import settings as default_settings
from sentinelai.platform.crypto.anchoring import (
    AnchorBatch,
    AnchoringError,
    LedgerAnchorService,
    PublishedAnchor,
)
from sentinelai.platform.crypto.kms import KeyManagementService
from sentinelai.platform.crypto.ledger import LEDGER_AUDIT, LEDGER_CUSTODY
from sentinelai.platform.crypto.metrics import (
    LEDGER_ANCHOR_BATCH_DURATION,
    LEDGER_ANCHORED_ENTRIES,
    LEDGER_ANCHORS_CUT,
)
from sentinelai.platform.crypto.tsa import build_timestamp_authority
from sentinelai.platform.crypto.verification import AnchorView
from sentinelai.platform.db.session import async_session_factory
from sentinelai.platform.logging import log
from sentinelai.platform.storage import build_object_storage
from sentinelai.platform.storage.port import ObjectStorage

# Never anchor anything newer than this. See the module docstring: it is the guard against
# ordinary clock skew reordering an entry into an already-committed range.
ANCHOR_WATERMARK_MINUTES: Final = 15

# Upper bound on one anchor's leaf count. A Merkle tree over a very large batch is cheap to build
# but expensive to *verify* against a chain read back into memory, and one enormous first anchor
# would also mean a single root covering years of history — losing the ability to say *when* a
# range was committed. Entries beyond the cap are picked up by the next run.
MAX_ENTRIES_PER_ANCHOR: Final = 10_000


def _unanchored_tail(chain: list[str], anchors: list[AnchorView]) -> list[str] | None:
    """The contiguous run of ``chain`` after the furthest position any anchor covers.

    Returns ``None`` to mean **refuse to cut**, which is different from returning ``[]`` (nothing
    new to anchor). Refusal happens when any existing anchor's ``last_entry_hash`` cannot be found
    in the current chain.

    That distinction is the most important thing in this module. An unresolvable anchor endpoint
    means the ledger no longer contains entries it was committed to — the signature of a
    truncation. A cutter that shrugged and treated the boundary as "nothing is anchored" would
    publish a *fresh*
    commitment over the surviving, doctored history: a valid anchor, correctly signed, attesting to
    the attacker's version of events. That would not merely fail to detect the truncation, it would
    launder it into proof. So the run stops and leaves the failing anchor on record for the
    Verification Engine to report.

    Position is resolved by locating each anchor's end and taking the *furthest* one, rather than
    trusting the newest anchor by timestamp — two anchors cut concurrently, or one re-cut after a
    partial failure, must never move the boundary backwards and re-anchor a committed range.
    """
    boundary = -1
    for anchor in anchors:
        try:
            boundary = max(boundary, chain.index(anchor.last_entry_hash))
        except ValueError:
            log.error(
                "anchor_end_missing_from_chain",
                anchor_id=str(anchor.anchor_id),
                ledger=anchor.ledger,
                detail=(
                    "the ledger no longer contains this anchor's last entry, so the safe cut "
                    "boundary is unknown; refusing to publish a new anchor over a possibly "
                    "truncated history"
                ),
            )
            return None
    return chain[boundary + 1 : boundary + 1 + MAX_ENTRIES_PER_ANCHOR]


def _anchor_row(anchor: PublishedAnchor) -> LedgerAnchor:
    """Map a published anchor onto its append-only row."""
    return LedgerAnchor(
        anchor_id=anchor.anchor_id,
        ledger=anchor.ledger,
        merkle_root=anchor.merkle_root,
        merkle_hash_algo=anchor.merkle_hash_algo,
        first_entry_hash=anchor.first_entry_hash,
        last_entry_hash=anchor.last_entry_hash,
        entry_count=anchor.entry_count,
        created_at=anchor.created_at,
        signature=anchor.signature.envelope,
        sig_alg=anchor.signature.sig_alg,
        key_id=anchor.signature.key_id,
        worm_object_ref=anchor.worm_object_ref,
        # RFC 3161 token (Wave 1.3c), base64 of the DER because the column is `Text`. Explicitly
        # null when timestamping is disabled, so a verifier meeting this row can tell "no token was
        # ever obtained" from "a token was obtained and is wrong" — different findings entirely.
        tsa_token_ref=(
            base64.b64encode(anchor.tsa_token).decode("ascii")
            if anchor.tsa_token is not None
            else None
        ),
    )


async def cut_anchor_batches(
    ctx: dict[str, Any], *, watermark_minutes: int = ANCHOR_WATERMARK_MINUTES
) -> None:
    """Cut and publish one anchor per evidentiary ledger, for whatever is newly unanchored.

    Idempotent by construction: the unanchored tail of a fully-anchored ledger is empty, and an
    empty batch is skipped rather than published (``AnchorBatch`` refuses one outright — anchoring
    nothing would publish a commitment to the empty tree and claim it covered a range). So a run
    against an already-anchored ledger is a no-op, and arq retrying a run that failed after
    publishing simply finds the new boundary and moves on.

    **Ordering inside a cut is the safety property**, and it is `LedgerAnchorService.publish`'s:
    the WORM object is written *before* the row is recorded. A row pointing at an object that was
    never written would be a database claiming an anchor exists when it does not — the precise lie
    this subsystem exists to make impossible. The reverse (an object with no row) is recoverable,
    because the object is self-describing.
    """
    session_factory = ctx.get("session_factory") or async_session_factory
    kms: KeyManagementService = ctx["kms"]
    storage: ObjectStorage = ctx.get("object_storage") or build_object_storage()
    settings: Settings = ctx.get("settings") or default_settings

    service = LedgerAnchorService(
        kms,
        storage,
        bucket=settings.storage_anchor_bucket,
        retention_years=settings.storage_anchor_retention_years,
        # None when TSA_ENABLED is false, which is the air-gapped configuration. The anchor cut
        # proceeds either way — timestamping is additive proof of *when*, and its absence does not
        # weaken the non-truncation guarantee WORM provides (ADR-0003 §3, Wave 1.3c).
        timestamp_authority=build_timestamp_authority(
            enabled=settings.tsa_enabled,
            url=settings.tsa_url,
            trust_anchors_pem=settings.tsa_trust_anchors_pem,
            hash_algo=settings.tsa_hash_algorithm,
            timeout_seconds=settings.tsa_timeout_seconds,
        ),
    )
    watermark = datetime.now(UTC) - timedelta(minutes=watermark_minutes)
    cut = 0

    async with session_factory() as session:
        uow = IngestionUnitOfWork(session)
        # Custody first: it is the chain a court asks about, so it gets the KMS round-trip if this
        # run is interrupted.
        for ledger in (LEDGER_CUSTODY, LEDGER_AUDIT):
            started = time.monotonic()
            # Each ledger's canonical global order is owned by whoever owns the table: `ingestion`
            # for custody, `platform` for audit. Both apply the same watermark.
            chain = (
                await uow.custody.chain_hashes(before=watermark)
                if ledger == LEDGER_CUSTODY
                else await read_audit_chain_hashes(session, before=watermark)
            )
            anchors = await read_anchor_views(session, ledger)
            tail = _unanchored_tail(chain, anchors)

            if tail is None:
                # An anchor names entries the ledger no longer holds. Not this job's problem to
                # report — that is the Verification Engine's — but absolutely this job's problem not
                # to paper over by publishing a fresh commitment over what survived.
                log.error(
                    "anchor_batch_refused_unresolvable_boundary",
                    ledger=ledger,
                    chain_length=len(chain),
                    existing_anchors=len(anchors),
                )
                continue

            if not tail:
                log.info(
                    "anchor_batch_skipped_empty",
                    ledger=ledger,
                    chain_length=len(chain),
                    existing_anchors=len(anchors),
                )
                continue

            try:
                published = await service.publish(
                    AnchorBatch(ledger=ledger, entry_hashes=tuple(tail))
                )
            except AnchoringError:
                # Nothing is recorded, so the range stays unanchored and the next run retries it.
                # Re-raised rather than swallowed: an anchor that could not be published is an
                # operational failure worth a retry and an alert, not a quiet skip.
                log.error("anchor_publish_failed", ledger=ledger, entry_count=len(tail))
                raise

            session.add(_anchor_row(published))
            await session.commit()
            cut += 1

            elapsed = time.monotonic() - started
            LEDGER_ANCHORS_CUT.labels(ledger=ledger).inc()
            LEDGER_ANCHORED_ENTRIES.labels(ledger=ledger).inc(len(tail))
            LEDGER_ANCHOR_BATCH_DURATION.labels(ledger=ledger).observe(elapsed)
            log.info(
                "anchor_batch_cut",
                ledger=ledger,
                anchor_id=str(published.anchor_id),
                entry_count=published.entry_count,
                merkle_root=published.merkle_root,
                worm_object_ref=published.worm_object_ref,
                duration_seconds=round(elapsed, 3),
            )

    log.info("anchor_batch_run_complete", anchors_cut=cut, watermark=watermark.isoformat())


__all__ = [
    "ANCHOR_WATERMARK_MINUTES",
    "MAX_ENTRIES_PER_ANCHOR",
    "cut_anchor_batches",
]
