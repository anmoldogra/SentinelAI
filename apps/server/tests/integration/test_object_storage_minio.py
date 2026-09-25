"""MinIO integration tests for the S3-compatible adapter (ADR-0008).

Runs the shared ObjectStorage contract against a real MinIO endpoint, and proves a presigned
download URL actually serves the stored bytes. Skips with a clear reason when MinIO is
unreachable or the configured ``STORAGE_*`` credentials don't authenticate — so a keyless
``make check`` never fails, while a provisioned MinIO gives real coverage.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import httpx
import pytest

from sentinelai.platform.config import settings
from sentinelai.platform.storage.factory import build_object_storage
from sentinelai.platform.storage.port import ObjectStorage
from sentinelai.platform.storage.worm import WormMisconfigured, verify_worm_bucket
from tests.contract.object_storage_contract import check_object_storage


async def _usable_storage() -> ObjectStorage | None:
    """Return a working adapter, or ``None`` if MinIO is unreachable/misconfigured."""
    storage = build_object_storage(settings)
    try:
        await storage.ensure_bucket(f"probe-{uuid.uuid4().hex[:8]}")
    except Exception:
        return None
    return storage


_SKIP = (
    f"MinIO not reachable/authenticated at {settings.storage_endpoint_url} "
    "— set STORAGE_ACCESS_KEY/STORAGE_SECRET_KEY to the cluster's credentials to run"
)


async def test_minio_adapter_satisfies_the_object_storage_contract() -> None:
    storage = await _usable_storage()
    if storage is None:
        pytest.skip(_SKIP)
    await check_object_storage(storage, bucket=f"test-storage-{uuid.uuid4().hex[:8]}")


async def test_presigned_download_url_serves_the_stored_bytes() -> None:
    storage = await _usable_storage()
    if storage is None:
        pytest.skip(_SKIP)

    bucket = f"test-presign-{uuid.uuid4().hex[:8]}"
    await storage.ensure_bucket(bucket)

    async def _payload() -> AsyncIterator[bytes]:
        yield b"presigned-evidence-bytes"

    await storage.put_stream(bucket, "obj", _payload())
    url = await storage.presigned_download_url(bucket, "obj", expires_in=300)
    async with httpx.AsyncClient(timeout=5.0) as client:
        response = await client.get(url)
    assert response.status_code == 200
    assert response.content == b"presigned-evidence-bytes"
    await storage.delete(bucket, "obj")


# ---------------------------------------------------------------------------------------
# WORM / Object Lock against a real MinIO — ADR-0003 §3.
#
# These close a real gap. `ensure_worm_bucket`, `put_immutable` and `object_lock_status` are the
# only code standing behind the entire truncation guarantee, and until now every anchoring test in
# this repository ran against `FakeObjectStorage`, which records a dict entry and has no concept of
# a lock. The mechanism was therefore unproven against any real S3 implementation, and a regression
# in these three methods would have been invisible to CI while every anchoring test stayed green.
# ---------------------------------------------------------------------------------------


async def test_ensure_worm_bucket_creates_a_bucket_with_object_lock_enabled() -> None:
    """Object Lock is fixed at creation, so this is the only moment it can be turned on."""
    storage = await _usable_storage()
    if storage is None:
        pytest.skip(_SKIP)

    bucket = f"test-worm-{uuid.uuid4().hex[:8]}"
    await storage.ensure_worm_bucket(bucket)

    status = await storage.object_lock_status(bucket)
    assert status.enabled is True
    # No bucket-default rule: this platform names COMPLIANCE per write instead, and the readiness
    # probe accepts exactly this shape.
    assert status.default_mode is None


async def test_an_ordinary_bucket_reports_no_object_lock() -> None:
    """The case the startup probe exists to catch, confirmed against a real server.

    MinIO signals "no Object Lock" by *erroring*, not by returning an empty configuration, so this
    also pins the adapter's translation of that error into `enabled=False` rather than a crash.
    """
    storage = await _usable_storage()
    if storage is None:
        pytest.skip(_SKIP)

    bucket = f"test-nolock-{uuid.uuid4().hex[:8]}"
    await storage.ensure_bucket(bucket)

    status = await storage.object_lock_status(bucket)
    assert status.enabled is False


async def test_the_readiness_probe_accepts_a_real_worm_bucket_and_rejects_a_plain_one() -> None:
    """End-to-end: the boot gate against real buckets, not a fake's dictionary."""
    storage = await _usable_storage()
    if storage is None:
        pytest.skip(_SKIP)

    worm_bucket = f"test-worm-ok-{uuid.uuid4().hex[:8]}"
    plain_bucket = f"test-worm-bad-{uuid.uuid4().hex[:8]}"
    await storage.ensure_worm_bucket(worm_bucket)
    await storage.ensure_bucket(plain_bucket)

    await verify_worm_bucket(storage, worm_bucket)  # must not raise

    with pytest.raises(WormMisconfigured):
        await verify_worm_bucket(storage, plain_bucket)


async def test_an_object_written_immutably_cannot_be_deleted_before_its_retention_expires() -> None:
    """The guarantee itself, proven by attempting the deletion COMPLIANCE mode must refuse.

    Retention is deliberately short (a few seconds) so the test bucket does not become undeletable
    for a decade — the *mode* is what is under test, not the duration. COMPLIANCE cannot be bypassed
    by any principal including the account root, so a successful delete here would mean the anchor
    guarantee is not real.
    """
    storage = await _usable_storage()
    if storage is None:
        pytest.skip(_SKIP)

    bucket = f"test-worm-lock-{uuid.uuid4().hex[:8]}"
    await storage.ensure_worm_bucket(bucket)
    key = "anchors/probe.json"
    body = b'{"merkle_root":"deadbeef"}'
    retain_until = datetime.now(UTC) + timedelta(seconds=5)

    await storage.put_immutable(
        bucket, key, body, retain_until=retain_until, content_type="application/json"
    )

    assert await storage.exists(bucket, key)

    # A plain delete places a delete marker (the object version survives), so the object must still
    # be readable by version afterwards. The load-bearing assertion is that the *version* cannot be
    # removed while the lock holds.
    with pytest.raises(Exception) as excinfo:
        await _delete_locked_version(storage, bucket, key)
    assert "InvalidRequest" in type(excinfo.value).__name__ or "InvalidRequest" in str(
        excinfo.value
    )

    # And the bytes are intact.
    chunks = [chunk async for chunk in storage.get_stream(bucket, key)]
    assert b"".join(chunks) == body


async def _delete_locked_version(storage: ObjectStorage, bucket: str, key: str) -> None:
    """Attempt a versioned delete, which is what Object Lock actually refuses.

    Reaches through the adapter to the raw client on purpose: the `ObjectStorage` port has no
    delete-by-version operation, and adding one solely to prove a lock holds would widen the
    production surface for a test's benefit.
    """
    minio = cast(Any, storage)
    async with minio._client() as s3:
        head = await s3.head_object(Bucket=bucket, Key=key)
        await s3.delete_object(Bucket=bucket, Key=key, VersionId=head["VersionId"])
