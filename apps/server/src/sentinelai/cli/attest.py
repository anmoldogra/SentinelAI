"""Independent evidentiary attestation — ADR-0003 §3/§6, deployment-architecture.md Part 14.

Answers one question about a database: **does it still contain everything the published anchors
committed to, and is every entry in it authentic?** It is the tool Part 14 requires be run
"after any restore of a database containing ``platform.audit_log`` or
``ingestion.evidence_custody_events`` ... before the system is returned to service", and the one an
auditor or a court-appointed examiner runs to check the claim for themselves.

Run it as a module::

    python -m sentinelai.cli.attest verify
    python -m sentinelai.cli.attest verify --ledger audit --json

**It stands alone.** No API server, no worker, no arq, no HTTP. It needs three things, all
read-only: ``DATABASE_URL`` pointed at the database under examination (a read-only role is
sufficient — nothing here writes), read access to the WORM anchor bucket, and *verify* access to the
KMS key the ledgers were signed under. The KMS requirement cannot be removed and should not be
wished away: a hash proves nothing about authorship, so a tool that skipped signature verification
would report a rewritten ledger as intact. It never uses a private key.

**Why it does not write an audit entry.** Every other privileged read in this platform is audited,
and this one deliberately is not. An audit write appends to one of the two ledgers under
examination — taking the chain lock, consuming a KMS *signing* operation, and mutating the evidence.
An examiner's read must not alter the thing being examined, and a tool that an auditor runs against
a restored database has no business needing write credentials at all. The same reasoning
``EvidenceService.reverify_custody_chain`` records for the scheduled job applies with more force
here, because here the caller may be someone outside the organisation.

**Anchors come from the bucket, never from the database.** This is the difference between this tool
and ``GET /api/v1/evidence/{id}/verify``. The online endpoint reads ``platform.ledger_anchors``,
which is correct for its purpose and catches an ordinary truncation. It cannot catch a *restore*,
because a point-in-time restore rolls the ledger and its anchor rows back together and leaves a
database that is internally flawless — every hash recomputes, every signature verifies, every anchor
it still remembers reconciles. See :mod:`sentinelai.platform.crypto.attestation`.

**Exit codes are the contract**, because a Kubernetes ``CronJob`` reads them and nothing else:

* ``0`` — verified. Also ``partial`` unless ``--strict``: ADR-0003's three states exist precisely so
  that "not provable" and "forged" are not the same alarm, and pre-signing legacy rows would
  otherwise page someone every night forever.
* ``2`` — **failed.** Something committed to is missing or altered. Part 14 calls this an
  evidentiary incident, not a restore defect to be tidied away.
* ``1`` — the check could not be completed (database unreachable, KMS down, bucket denied). "We
  could not check" and "the ledger is broken" are opposite conclusions, and conflating them would
  train an operator to ignore the one that matters.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final

from cryptography import x509
from sqlalchemy.ext.asyncio import AsyncSession

from sentinelai.modules.ingestion.repository import IngestionUnitOfWork
from sentinelai.modules.ingestion.service import custody_entry_views
from sentinelai.platform.auth.ledger_verification import (
    DEFAULT_AUDIT_ENTRY_WINDOW,
    read_anchor_views,
    read_audit_chain_hashes,
    read_recent_audit_entries,
)
from sentinelai.platform.config import settings
from sentinelai.platform.crypto import create_kms
from sentinelai.platform.crypto.attestation import (
    AttestationReport,
    WormAnchorReader,
    build_attestation_report,
    reconcile_anchors,
    verify_document_signatures,
)
from sentinelai.platform.crypto.kms import KeyManagementService
from sentinelai.platform.crypto.ledger import LEDGER_AUDIT, LEDGER_CUSTODY, LedgerSigner
from sentinelai.platform.crypto.tsa import load_trust_anchors
from sentinelai.platform.crypto.verification import (
    LedgerVerificationReport,
    LedgerVerifier,
    VerificationState,
)
from sentinelai.platform.db.session import async_session_factory, dispose_engine
from sentinelai.platform.storage import build_object_storage
from sentinelai.platform.storage.port import ObjectStorage

EXIT_VERIFIED: Final = 0
EXIT_CANNOT_CHECK: Final = 1
EXIT_FAILED: Final = 2

# How many custody chains one run verifies at entry level, most-recently-active first. The anchor
# layer covers the WHOLE custody ledger regardless of this number, so truncation detection is never
# bounded by it — only the per-entry hash and signature sweep is. Matches the scheduled job's
# default so a CronJob and an hourly re-verification agree about what "checked" means.
DEFAULT_CUSTODY_CHAIN_BUDGET: Final = 250

_LEDGERS: Final[dict[str, str]] = {"audit": LEDGER_AUDIT, "custody": LEDGER_CUSTODY}


@dataclass(frozen=True, slots=True)
class LedgerAttestation:
    """Both halves of one ledger's verdict: the chain, and the archive that commits to it."""

    ledger: str
    chain: LedgerVerificationReport
    archive: AttestationReport
    entry_chains_checked: int = 1

    @property
    def state(self) -> VerificationState:
        for state in (VerificationState.FAILED, VerificationState.PARTIAL):
            if state in (self.chain.state, self.archive.state):
                return state
        return VerificationState.VERIFIED


async def attest_ledger(
    *,
    ledger: str,
    session: AsyncSession,
    signer: LedgerSigner,
    kms: KeyManagementService,
    storage: ObjectStorage,
    bucket: str,
    audit_entry_window: int = DEFAULT_AUDIT_ENTRY_WINDOW,
    custody_chain_budget: int = DEFAULT_CUSTODY_CHAIN_BUDGET,
    tsa_trust_anchors: Sequence[x509.Certificate] = (),
) -> LedgerAttestation:
    """Verify one ledger's chain against the anchors published in WORM, then reconcile the stores.

    The anchor layer runs over the ledger's **complete** ordered entry-hash list for both ledgers,
    never over a window. That scope is not an optimisation: an anchor exists to catch a deleted
    tail, and a deleted tail is by definition not among the rows that survived.

    Every dependency arrives as an argument — bucket, storage, KMS, trust anchors — rather than
    being read from ``settings`` here. That is what lets ``test_attestation_db.py`` run this exact
    function against a scratch database and a scratch bucket: an attestation tool whose integration
    test exercised a reimplementation of it would be testing the wrong code.
    """
    reader = WormAnchorReader(storage, bucket=bucket)
    documents, malformed = await reader.read(ledger)
    database_anchors = await read_anchor_views(session, ledger)
    archive = build_attestation_report(
        ledger=ledger,
        attestations=reconcile_anchors(
            documents=documents,
            malformed=malformed,
            database_anchors=database_anchors,
            signature_valid=await verify_document_signatures(signer, documents),
        ),
        database_anchors=len(database_anchors),
    )

    verifier = LedgerVerifier(signer, tsa_trust_anchors=tsa_trust_anchors)
    # The anchors handed to the verifier are the bucket's, not the database's. That substitution is
    # the entire reason this tool exists.
    anchors = [document.to_anchor_view() for document in documents]

    if ledger == LEDGER_AUDIT:
        chain = await verifier.verify_chain(
            ledger=ledger,
            entries=await read_recent_audit_entries(session, limit=audit_entry_window),
            anchors=anchors,
            chain_entry_hashes=await read_audit_chain_hashes(session),
            # A window into a global, unbounded ledger does not begin at genesis.
            expect_genesis=False,
        )
        return LedgerAttestation(ledger=ledger, chain=chain, archive=archive)

    uow = IngestionUnitOfWork(session, kms=kms)
    chain_hashes = await uow.custody.chain_hashes()
    # Custody's anchor layer is checked over the global order with no entries, because the custody
    # ledger is not one chain: every evidence item's chain starts at the all-zero sentinel, so
    # walking the global order for link continuity would report a break at every item boundary.
    # Entry-level checks therefore run per chain, below, and the two phases are merged.
    chain = await verifier.verify_chain(
        ledger=ledger, entries=(), anchors=anchors, chain_entry_hashes=chain_hashes
    )
    evidence_ids = await uow.custody.recently_active_evidence_ids(limit=custody_chain_budget)
    merged = chain
    for evidence_id in evidence_ids:
        per_chain = await verifier.verify_chain(
            ledger=ledger,
            entries=custody_entry_views(await uow.custody.list_for_evidence(evidence_id)),
            # No anchors here: they were checked once, above, against the global order. Passing them
            # per chain would report every other item's anchor as an unlocatable range.
            anchors=(),
            expect_genesis=True,
        )
        merged = _merge_reports(merged, per_chain)
    return LedgerAttestation(
        ledger=ledger, chain=merged, archive=archive, entry_chains_checked=len(evidence_ids)
    )


def _merge_reports(
    base: LedgerVerificationReport, other: LedgerVerificationReport
) -> LedgerVerificationReport:
    """Fold a per-chain custody report into the running total for the ledger.

    Entry lists are **not** concatenated. A full custody sweep can cover hundreds of chains and tens
    of thousands of entries, and an attestation report that carried every one of them would be
    unreadable on a terminal and large enough to matter in a log. The counts and the findings are
    what a verdict rests on; the per-entry detail for a specific item is what
    ``GET /api/v1/evidence/{id}/verify`` is for.
    """
    states = (base.state, other.state)
    return LedgerVerificationReport(
        ledger=base.ledger,
        state=(
            VerificationState.FAILED
            if VerificationState.FAILED in states
            else VerificationState.PARTIAL
            if VerificationState.PARTIAL in states
            else VerificationState.VERIFIED
        ),
        entry_count=base.entry_count + other.entry_count,
        verified_entries=base.verified_entries + other.verified_entries,
        partial_entries=base.partial_entries + other.partial_entries,
        failed_entries=base.failed_entries + other.failed_entries,
        entries=(),
        anchors=base.anchors + other.anchors,
        unanchored_entries=base.unanchored_entries + other.unanchored_entries,
        untimestamped_anchors=base.untimestamped_anchors,
        findings=tuple(sorted(set(base.findings) | set(other.findings))),
    )


def _as_dict(attestation: LedgerAttestation) -> dict[str, Any]:
    chain, archive = attestation.chain, attestation.archive
    return {
        "ledger": attestation.ledger,
        "state": str(attestation.state),
        "chain": {
            "state": str(chain.state),
            "entries_checked": chain.entry_count,
            "verified_entries": chain.verified_entries,
            "partial_entries": chain.partial_entries,
            "failed_entries": chain.failed_entries,
            "entry_chains_checked": attestation.entry_chains_checked,
            "anchors_checked": len(chain.anchors),
            "unanchored_entries": chain.unanchored_entries,
            "untimestamped_anchors": chain.untimestamped_anchors,
            "findings": [str(f) for f in chain.findings],
        },
        "archive": {
            "state": str(archive.state),
            "worm_anchors": archive.worm_anchors,
            "database_anchors": archive.database_anchors,
            "reconciled": archive.reconciled,
            "missing_from_database": archive.missing_from_database,
            "missing_from_worm": archive.missing_from_worm,
            "malformed_objects": archive.malformed_objects,
            "findings": [str(f) for f in archive.findings],
            "anomalies": [
                {
                    "object_key": a.object_key,
                    "anchor_id": str(a.anchor_id) if a.anchor_id else None,
                    "state": str(a.state),
                    "findings": [str(f) for f in a.findings],
                    "detail": a.detail,
                }
                for a in archive.anchors
                if a.state is not VerificationState.VERIFIED
            ],
        },
    }


def _print_human(attestations: Sequence[LedgerAttestation], overall: VerificationState) -> None:
    print(f"anchor bucket: {settings.storage_anchor_bucket}")
    for attestation in attestations:
        chain, archive = attestation.chain, attestation.archive
        print(f"\n{attestation.ledger}  [{attestation.state}]")
        print(
            f"  chain   : {chain.entry_count} entries checked "
            f"({chain.verified_entries} verified, {chain.partial_entries} partial, "
            f"{chain.failed_entries} failed) over {attestation.entry_chains_checked} chain(s); "
            f"{len(chain.anchors)} published anchor(s) reconciled against the ledger"
        )
        if chain.findings:
            print(f"            findings: {', '.join(str(f) for f in chain.findings)}")
        print(
            f"  archive : {archive.worm_anchors} anchor(s) in WORM, "
            f"{archive.database_anchors} row(s) in the database, {archive.reconciled} agreeing"
        )
        for anomaly in archive.anchors:
            if anomaly.state is VerificationState.VERIFIED:
                continue
            print(f"            ! {anomaly.object_key}: {', '.join(anomaly.findings)}")
            if anomaly.detail:
                print(f"              {anomaly.detail}")
        if chain.unanchored_entries:
            # Not a defect: anchoring is batched on a 15-minute watermark, so a healthy ledger
            # always has a small non-zero tail. Sustained growth is the alarm, which is the gauge's
            # job, not this tool's.
            print(f"            {chain.unanchored_entries} entry(s) not yet anchored")

    print(f"\nattestation: {overall.upper()}")
    if overall is VerificationState.FAILED:
        print(
            "This database no longer contains everything its published anchors committed to, or\n"
            "an entry in it is not authentic. deployment-architecture.md Part 14: treat this as\n"
            "an evidentiary incident (security-architecture.md §48), not a restore defect. Any\n"
            "case relying on the affected entries needs to know."
        )


async def verify(
    *,
    ledgers: Sequence[str],
    audit_entry_window: int,
    custody_chain_budget: int,
    as_json: bool,
    strict: bool,
) -> int:
    """Attest the requested ledgers and return the process exit code."""
    kms: KeyManagementService = create_kms(settings)
    await kms.start()
    storage = build_object_storage()
    try:
        signer = LedgerSigner(kms)
        attestations: list[LedgerAttestation] = []
        async with async_session_factory() as session:
            for name in ledgers:
                attestations.append(
                    await attest_ledger(
                        ledger=_LEDGERS[name],
                        session=session,
                        signer=signer,
                        kms=kms,
                        storage=storage,
                        bucket=settings.storage_anchor_bucket,
                        audit_entry_window=audit_entry_window,
                        custody_chain_budget=custody_chain_budget,
                        tsa_trust_anchors=load_trust_anchors(settings.tsa_trust_anchors_pem),
                    )
                )
    finally:
        await kms.aclose()

    states = [a.state for a in attestations]
    overall = (
        VerificationState.FAILED
        if VerificationState.FAILED in states
        else VerificationState.PARTIAL
        if VerificationState.PARTIAL in states
        else VerificationState.VERIFIED
    )

    if as_json:
        print(
            json.dumps(
                {
                    "tool": "sentinelai-attest",
                    "anchor_bucket": settings.storage_anchor_bucket,
                    "state": str(overall),
                    "ledgers": [_as_dict(a) for a in attestations],
                },
                indent=2,
                sort_keys=True,
            )
        )
    else:
        _print_human(attestations, overall)

    if overall is VerificationState.FAILED:
        return EXIT_FAILED
    if overall is VerificationState.PARTIAL and strict:
        return EXIT_FAILED
    return EXIT_VERIFIED


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m sentinelai.cli.attest",
        description=(
            "Verify an evidentiary database against the Merkle roots published to the WORM anchor "
            "bucket. Read-only; safe to run against a restored backup or a DR site."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("verify", help="Attest the evidentiary ledgers (ADR-0003 §3/§6)")
    run.add_argument(
        "--ledger",
        choices=("all", "audit", "custody"),
        default="all",
        help="Which ledger to attest. Default: all.",
    )
    run.add_argument(
        "--audit-entry-window",
        type=int,
        default=DEFAULT_AUDIT_ENTRY_WINDOW,
        help=(
            "How many of the most recent audit entries to hash- and signature-check. The anchor "
            "layer covers the whole ledger regardless. "
            f"Default: {DEFAULT_AUDIT_ENTRY_WINDOW}."
        ),
    )
    run.add_argument(
        "--custody-chain-budget",
        type=int,
        default=DEFAULT_CUSTODY_CHAIN_BUDGET,
        help=(
            "How many custody chains to check at entry level, most recently active first. The "
            f"anchor layer covers the whole ledger regardless. Default: "
            f"{DEFAULT_CUSTODY_CHAIN_BUDGET}."
        ),
    )
    run.add_argument("--json", action="store_true", help="Emit a machine-readable report.")
    run.add_argument(
        "--strict",
        action="store_true",
        help=(
            "Exit non-zero on 'partial' as well as 'failed'. Off by default so unsigned "
            "pre-Wave-1.2 history does not raise a standing alarm that has no remedy."
        ),
    )
    return parser


async def _run(args: argparse.Namespace) -> int:
    # The same fail-closed profile check both long-running entrypoints make before opening a
    # connection: an attestation run against a production database with a half-configured profile
    # would produce a verdict nobody should rely on.
    settings.validate_for_profile()
    ledgers = ("audit", "custody") if args.ledger == "all" else (args.ledger,)
    try:
        return await verify(
            ledgers=ledgers,
            audit_entry_window=args.audit_entry_window,
            custody_chain_budget=args.custody_chain_budget,
            as_json=args.json,
            strict=args.strict,
        )
    finally:
        await dispose_engine()


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return asyncio.run(_run(args))
    except Exception as exc:
        # Everything that is not a verdict lands here and becomes EXIT_CANNOT_CHECK. A tool whose
        # crash looked like a clean ledger would be worse than no tool; one whose crash looked like
        # tampering would page an operator to investigate a network fault.
        print(f"attestation could not be completed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_CANNOT_CHECK


if __name__ == "__main__":
    raise SystemExit(main())
