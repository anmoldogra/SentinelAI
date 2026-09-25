"""The WORM startup probe — ADR-0003 §3, deployment-architecture.md Part 7.

The probe exists because `ensure_worm_bucket` deliberately does not verify an *existing* bucket: S3
Object Lock is fixed at creation, so the application cannot repair a bucket that lacks it. That
makes
a pre-existing, correctly-named, non-WORM bucket the dangerous case — creation is skipped, writes
may
succeed, and every anchor published into it is deletable by the insider anchoring defends against,
with nothing in the data to reveal it.

These tests pin the two refusals and, just as importantly, the two acceptances: a probe that
rejected
a correctly-configured bucket would block every deploy, which is the fastest route to someone
deleting the check.
"""

from __future__ import annotations

import pytest

from sentinelai.platform.storage.exceptions import BucketNotFound
from sentinelai.platform.storage.port import ObjectLockStatus
from sentinelai.platform.storage.worm import WormMisconfigured, verify_worm_bucket
from tests.fixtures.fake_object_storage import FakeObjectStorage

_BUCKET = "sentinelai-anchors"


async def test_a_bucket_created_worm_passes() -> None:
    """The happy path: `ensure_worm_bucket` made it, so Object Lock is on."""
    storage = FakeObjectStorage()
    await storage.ensure_worm_bucket(_BUCKET)

    await verify_worm_bucket(storage, _BUCKET)  # must not raise


async def test_a_bucket_with_a_compliance_default_rule_passes() -> None:
    """A COMPLIANCE default is stricter than we need, not a conflict."""
    storage = FakeObjectStorage()
    await storage.ensure_bucket(_BUCKET)
    storage.lock_status[_BUCKET] = ObjectLockStatus(enabled=True, default_mode="COMPLIANCE")

    await verify_worm_bucket(storage, _BUCKET)


async def test_a_bucket_with_no_default_rule_passes() -> None:
    """The real configuration this platform ships.

    `put_immutable` names COMPLIANCE on every write it makes, so the bucket default only governs
    writes that name no mode — and this subsystem makes none. Rejecting the absence of a default
    would
    reject the correct setup.
    """
    storage = FakeObjectStorage()
    await storage.ensure_bucket(_BUCKET)
    storage.lock_status[_BUCKET] = ObjectLockStatus(enabled=True, default_mode=None)

    await verify_worm_bucket(storage, _BUCKET)


async def test_a_bucket_without_object_lock_is_refused() -> None:
    """The dangerous case: it exists, writes may work, and it proves nothing."""
    storage = FakeObjectStorage()
    await storage.ensure_bucket(_BUCKET)  # the ORDINARY path - no Object Lock

    with pytest.raises(WormMisconfigured) as excinfo:
        await verify_worm_bucket(storage, _BUCKET)

    message = str(excinfo.value)
    # The message has to tell an operator what to actually do, because the bucket cannot be
    # repaired.
    assert "Object Lock" in message
    assert "cannot be added afterwards" in message
    assert "storage_anchor_bucket" in message


async def test_a_governance_mode_bucket_is_refused() -> None:
    """GOVERNANCE is the subtle failure, and it is the one worth a dedicated test.

    Object Lock is genuinely on, so a naive check passes. But governance retention is bypassable by
    any principal holding `s3:BypassGovernanceRetention` — precisely the privileged insider the
    anchors defend against. An anchor a sufficiently-privileged operator can delete anchors nothing.
    """
    storage = FakeObjectStorage()
    await storage.ensure_bucket(_BUCKET)
    storage.lock_status[_BUCKET] = ObjectLockStatus(enabled=True, default_mode="GOVERNANCE")

    with pytest.raises(WormMisconfigured) as excinfo:
        await verify_worm_bucket(storage, _BUCKET)

    assert "GOVERNANCE" in str(excinfo.value)
    assert "BypassGovernanceRetention" in str(excinfo.value)


async def test_an_unreachable_bucket_is_not_reported_as_misconfigured() -> None:
    """ "We could not check" must never surface as "it is misconfigured".

    The operator responses are different — one is a provisioning fix, the other a retry — so the
    probe lets other storage failures through as their own type rather than flattening them.
    """
    storage = FakeObjectStorage()  # bucket never created

    with pytest.raises(BucketNotFound):
        await verify_worm_bucket(storage, _BUCKET)
