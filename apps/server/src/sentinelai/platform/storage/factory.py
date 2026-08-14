"""Object-storage factory (ADR-0008). Builds the configured ``ObjectStorage`` from ``Settings``.

Provider selection is by configuration: today the only implementation is the S3-compatible adapter
(MinIO on-prem / S3 in cloud). The composition root calls this once and injects the result.
"""

from __future__ import annotations

import aioboto3

from sentinelai.platform.config import Settings
from sentinelai.platform.storage.minio import MinioObjectStorage
from sentinelai.platform.storage.port import ObjectStorage


def build_object_storage(settings: Settings) -> ObjectStorage:
    """Construct the S3-compatible ``ObjectStorage`` adapter from settings."""
    return MinioObjectStorage(
        aioboto3.Session(),
        endpoint_url=settings.storage_endpoint_url,
        access_key=settings.storage_access_key.get_secret_value(),
        secret_key=settings.storage_secret_key.get_secret_value(),
    )
