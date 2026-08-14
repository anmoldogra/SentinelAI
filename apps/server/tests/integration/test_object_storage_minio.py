"""MinIO integration tests for the S3-compatible adapter (ADR-0008).

Runs the shared ObjectStorage contract against a real MinIO endpoint, and proves a presigned
download URL actually serves the stored bytes. Skips with a clear reason when MinIO is
unreachable or the configured ``STORAGE_*`` credentials don't authenticate — so a keyless
``make check`` never fails, while a provisioned MinIO gives real coverage.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import httpx
import pytest

from sentinelai.platform.config import settings
from sentinelai.platform.storage.factory import build_object_storage
from sentinelai.platform.storage.port import ObjectStorage
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
