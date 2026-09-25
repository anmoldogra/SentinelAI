"""Scheduled evidentiary re-verification — ADR-0003 §6(b), Wave 1.4.

Waves 1.1-1.3 made tampering *detectable*. Until something actually looks, it is not *detected*:
every guarantee those waves built is constructive, and a forged or truncated ledger sitting in the
database is indistinguishable from an intact one to anyone who never re-reads it. This job is what
closes that gap on a timer, and it is the difference between "we could prove this in court if asked"
and "we would know".

**Why this job lives in ``ingestion`` and verifies both ledgers.** ``ingestion`` owns
``evidence_custody_events``, so the custody half is plainly its own. The audit half is reached
through :class:`~sentinelai.platform.auth.ledger_verification.AuditLedgerVerificationService`, a
``platform`` service — and calling a platform service is not a boundary crossing, it is what
``platform`` is for. The alternative, splitting this into two jobs on two schedules, would let one
half silently stop while the other kept reporting green.

**What "alarm" means here.** ADR-0003 §6(b) requires the job to alarm on any break. In this platform
that is a Prometheus metric an Alertmanager rule fires on, plus a ``CRITICAL`` structured log line —
``deployment-architecture.md`` commits to the Prometheus/Grafana/Loki stack, and those two signals
are what an on-call operator actually receives.

It is deliberately **not** a ``notification``-module message. Every dispatch path in that module
requires an explicit ``recipient_user_id`` taken from an event payload, and a ledger integrity
failure has no user in its domain — it is addressed to security operations. There is no by-role user
lookup anywhere in this codebase and ``NotificationRule`` resolution is still unbuilt, so a
notification handler for this would resolve zero recipients on every firing: code that looks like
alerting while reaching nobody, which is strictly worse than no alerting at all. When recipient
resolution exists, this job is where it gets wired in.

**Failure of the job is not failure of the ledger.** A run that cannot complete (KMS down,
database unreachable) logs an error and re-raises so arq retries it. It must never emit a
``failed`` verdict, because "we could not check" and "the ledger is broken" are opposite
conclusions, and conflating them would train operators to ignore the one alarm that matters.
"""

from __future__ import annotations

import time
from typing import Any, Final

from sentinelai.modules.ingestion.repository import IngestionUnitOfWork
from sentinelai.modules.ingestion.service import EvidenceService
from sentinelai.platform.auth.ledger_verification import (
    AuditLedgerVerificationService,
    summarize_findings,
)
from sentinelai.platform.config import settings as default_settings
from sentinelai.platform.crypto.ledger import LEDGER_CUSTODY, LedgerSigner
from sentinelai.platform.crypto.metrics import (
    LEDGER_UNANCHORED_ENTRIES,
    LEDGER_VERIFICATION_DURATION,
    LEDGER_VERIFICATION_FINDINGS,
    LEDGER_VERIFICATION_STATE,
    LEDGER_VERIFICATIONS,
)
from sentinelai.platform.crypto.tsa import load_trust_anchors
from sentinelai.platform.crypto.verification import (
    LedgerVerificationReport,
    VerificationState,
)
from sentinelai.platform.logging import log
from sentinelai.platform.storage import build_object_storage

# How many custody chains one run re-verifies, most-recently-active first. The API verifies any
# chain on demand, and the anchor layer covers the whole ledger regardless of this number, so this
# bounds the *entry-level* sweep only.
DEFAULT_CUSTODY_CHAIN_BUDGET: Final = 250

# Gauge encoding for the last verdict per ledger. Numeric because Prometheus gauges are numeric, and
# ordered by severity so an alert rule can be written as `> 0` (something is not fully proven) or
# `>= 2` (something is positively wrong).
_STATE_GAUGE: Final[dict[VerificationState, int]] = {
    VerificationState.VERIFIED: 0,
    VerificationState.PARTIAL: 1,
    VerificationState.FAILED: 2,
}


def _record(report: LedgerVerificationReport, *, elapsed: float) -> None:
    """Publish one report to the metrics that constitute the alarm, then log it.

    Every report is recorded, not just the bad ones. A monitoring surface that only emits on failure
    cannot distinguish "healthy" from "the job stopped running", which is the failure mode this job
    is most likely to suffer and least likely to notice.
    """
    ledger = report.ledger
    LEDGER_VERIFICATIONS.labels(ledger=ledger, state=str(report.state)).inc()
    LEDGER_VERIFICATION_STATE.labels(ledger=ledger).set(_STATE_GAUGE[report.state])
    LEDGER_VERIFICATION_DURATION.labels(ledger=ledger).observe(elapsed)
    LEDGER_UNANCHORED_ENTRIES.labels(ledger=ledger).set(report.unanchored_entries)

    findings = summarize_findings(report)
    for finding, count in findings.items():
        LEDGER_VERIFICATION_FINDINGS.labels(ledger=ledger, finding=finding).inc(count)

    fields: dict[str, Any] = {
        "ledger": ledger,
        "state": str(report.state),
        "entry_count": report.entry_count,
        "verified_entries": report.verified_entries,
        "partial_entries": report.partial_entries,
        "failed_entries": report.failed_entries,
        "unanchored_entries": report.unanchored_entries,
        "anchors_checked": len(report.anchors),
        "findings": findings,
    }
    if report.is_failed:
        # CRITICAL, and phrased as a statement of fact rather than a warning: by the time this
        # fires, something has already altered or removed evidence that was cryptographically
        # committed to. `deployment-architecture.md`'s restore guidance treats this as an
        # evidentiary incident, not a maintenance ticket.
        log.critical("ledger_verification_failed", **fields)
    elif report.state is VerificationState.PARTIAL:
        log.info("ledger_verification_partial", **fields)
    else:
        log.info("ledger_verification_passed", **fields)


async def reverify_evidentiary_ledgers(
    ctx: dict[str, Any], *, custody_chain_budget: int = DEFAULT_CUSTODY_CHAIN_BUDGET
) -> None:
    """Re-verify the audit ledger and the most recently active custody chains.

    Read-only throughout: it issues ``SELECT``s and KMS *verify* calls (which touch no private key)
    and writes nothing to either ledger, so it cannot disturb what it is judging. There is
    consequently no ``uow.commit()`` here and no transaction to own — an intentional departure from
    the write-path jobs alongside it.

    Verification of one chain never aborts the sweep. A chain whose verdict is ``failed`` is
    recorded and the run continues, because the second failing chain is at least as interesting as
    the first and stopping early would hide it.
    """
    session_factory = ctx["session_factory"]
    kms = ctx["kms"]
    signer = LedgerSigner(kms)
    storage = ctx.get("object_storage") or build_object_storage()
    settings = ctx.get("settings") or default_settings

    failed_ledgers = 0
    async with session_factory() as session:
        # --- the audit ledger: windowed entries, anchors over the whole chain ---
        started = time.monotonic()
        audit_report = await AuditLedgerVerificationService(
            session, signer, tsa_trust_anchors=load_trust_anchors(settings.tsa_trust_anchors_pem)
        ).verify()
        _record(audit_report, elapsed=time.monotonic() - started)
        failed_ledgers += int(audit_report.is_failed)

        # --- custody chains: most recently active first, bounded ---
        uow = IngestionUnitOfWork(session)
        service = EvidenceService(uow, storage=storage, kms=kms)
        evidence_ids = await uow.custody.recently_active_evidence_ids(limit=custody_chain_budget)

        failed_chains = 0
        partial_chains = 0
        chain_started = time.monotonic()
        for evidence_id in evidence_ids:
            report = await service.reverify_custody_chain(evidence_id)
            if report.is_failed:
                failed_chains += 1
                # Per-chain CRITICAL, carrying the evidence id: the aggregate count tells an
                # operator something is wrong, but only the id tells them which case is affected.
                log.critical(
                    "custody_chain_verification_failed",
                    evidence_id=str(evidence_id),
                    state=str(report.state),
                    failed_entries=report.failed_entries,
                    findings=summarize_findings(report),
                )
            elif report.state is VerificationState.PARTIAL:
                partial_chains += 1

        # One rolled-up metric sample for the custody ledger. Per-chain gauges would be unbounded
        # cardinality (one label value per evidence item), which is the standard way to bring down a
        # Prometheus server; the per-chain detail lives in the log lines above instead.
        elapsed = time.monotonic() - chain_started
        LEDGER_VERIFICATION_DURATION.labels(ledger=LEDGER_CUSTODY).observe(elapsed)
        rolled_up = (
            VerificationState.FAILED
            if failed_chains
            else VerificationState.PARTIAL
            if partial_chains
            else VerificationState.VERIFIED
        )
        LEDGER_VERIFICATIONS.labels(ledger=LEDGER_CUSTODY, state=str(rolled_up)).inc()
        LEDGER_VERIFICATION_STATE.labels(ledger=LEDGER_CUSTODY).set(_STATE_GAUGE[rolled_up])
        failed_ledgers += int(bool(failed_chains))

        log.info(
            "custody_chains_verified",
            chains_checked=len(evidence_ids),
            failed_chains=failed_chains,
            partial_chains=partial_chains,
            state=str(rolled_up),
            duration_seconds=round(elapsed, 3),
        )

    # Completing normally after finding tampering is correct: the job did its job. The alarm is the
    # metric and the CRITICAL log, not an exception — raising here would make arq retry a run whose
    # verdict will not change, and eventually dead-letter it, turning a standing alarm into silence.
    log.info("evidentiary_reverification_complete", failed_ledgers=failed_ledgers)


__all__ = ["DEFAULT_CUSTODY_CHAIN_BUDGET", "reverify_evidentiary_ledgers"]
