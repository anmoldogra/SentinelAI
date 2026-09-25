"""WORM bucket readiness — ADR-0003 §3, deployment-architecture.md Part 7.

External anchoring only defeats truncation because the anchor is published somewhere the
database's administrator cannot rewrite. If the anchor bucket turns out not to be WORM, every
guarantee built on it silently degrades to nothing: anchors are still written, still signed, still
recorded — and still deletable by the exact insider they exist to defend against. Nothing in the
data would reveal it.

``ensure_worm_bucket`` creates the bucket with Object Lock when it is absent, but it explicitly
does **not** verify an existing one, because Object Lock is fixed at creation and cannot be
repaired by the application. A pre-existing bucket carrying the anchor bucket's name and no Object
Lock is therefore the dangerous case: creation is skipped, writes may well succeed, and the
deployment looks healthy.

This module is the check that closes it, and it belongs at startup rather than at first write for
one reason: a misconfiguration discovered on the first anchor write is discovered by a background
job, hours after the deploy, in a log line nobody is watching. A misconfiguration discovered at
startup stops the rollout.

**Two conditions, both required.**

1. Object Lock must be **enabled** on the bucket.
2. If the bucket carries a *default* retention rule, that rule must be ``COMPLIANCE``. GOVERNANCE
   is not an acceptable substitute — it is bypassable by any principal holding
   ``s3:BypassGovernanceRetention``, and an anchor a sufficiently-privileged operator can delete
   anchors nothing. A bucket with *no* default rule passes, because
   :meth:`~sentinelai.platform.storage.port.ObjectStorage.put_immutable` names COMPLIANCE on
   every write it makes; the default only governs writes that name no mode, and this subsystem
   makes none.
"""

from __future__ import annotations

from sentinelai.platform.logging import log
from sentinelai.platform.storage.exceptions import StorageError
from sentinelai.platform.storage.port import ObjectStorage

COMPLIANCE = "COMPLIANCE"
GOVERNANCE = "GOVERNANCE"


class WormMisconfigured(StorageError):
    """The anchor bucket cannot provide the WORM guarantee ADR-0003 §3 depends on.

    A subclass of ``StorageError`` so it travels through the existing storage taxonomy, but it is
    deliberately its own type: callers at startup must be able to distinguish "the anchor bucket is
    not WORM" — an unfixable provisioning fault that should stop a rollout — from a transient
    storage failure that a retry might clear.
    """


async def verify_worm_bucket(storage: ObjectStorage, bucket: str) -> None:
    """Raise :class:`WormMisconfigured` unless ``bucket`` can hold COMPLIANCE-mode anchors.

    Raises rather than returning a verdict because there is no useful degraded mode: a caller that
    received ``False`` and carried on would be running the anchoring subsystem knowing it proves
    nothing. Any other storage failure (unreachable endpoint, denied credentials) propagates as its
    own ``StorageError`` — "we could not check" must never be reported as "it is misconfigured",
    since the operator responses are different.
    """
    status = await storage.object_lock_status(bucket)

    if not status.enabled:
        raise WormMisconfigured(
            f"anchor bucket '{bucket}' does not have S3 Object Lock enabled. Object Lock is fixed "
            "at bucket creation and cannot be added afterwards, so this bucket can never hold "
            "tamper-proof anchors: provision a new bucket with ObjectLockEnabledForBucket and "
            "point storage_anchor_bucket at it (deployment-architecture.md Part 7)."
        )

    if status.default_mode == GOVERNANCE:
        raise WormMisconfigured(
            f"anchor bucket '{bucket}' has a GOVERNANCE-mode default retention rule. GOVERNANCE "
            "retention is bypassable by any principal holding s3:BypassGovernanceRetention, which "
            "is exactly the privileged insider anchoring defends against. Set the bucket's default "
            "retention mode to COMPLIANCE, or remove the default rule entirely (ADR-0003 §3)."
        )

    log.info(
        "worm_bucket_verified",
        bucket=bucket,
        object_lock="enabled",
        default_retention_mode=status.default_mode or "none (per-object COMPLIANCE)",
    )


__all__ = ["COMPLIANCE", "GOVERNANCE", "WormMisconfigured", "verify_worm_bucket"]
