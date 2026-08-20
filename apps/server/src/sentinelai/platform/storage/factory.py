"""Object-storage factory (ADR-0008). Builds the configured ``ObjectStorage`` from ``Settings``.

Provider selection is by configuration: today the only implementation is the S3-compatible adapter
(MinIO on-prem / S3 in cloud). Mirrors ``platform.crypto``'s composition shape — ``build_*`` is
called once by the composition root (``entrypoints/http/main.py`` lifespan), and ``get_*`` is the
FastAPI dependency that hands the already-built instance to request handlers.
"""

from __future__ import annotations

import aioboto3
from fastapi import Request

from sentinelai.platform.config import Settings
from sentinelai.platform.config import settings as default_settings
from sentinelai.platform.storage.minio import MinioObjectStorage
from sentinelai.platform.storage.port import ObjectStorage


def build_object_storage(cfg: Settings | None = None) -> ObjectStorage:
    """Construct the S3-compatible ``ObjectStorage`` adapter from settings."""
    cfg = cfg or default_settings
    return MinioObjectStorage(
        aioboto3.Session(),
        endpoint_url=cfg.storage_endpoint_url,
        access_key=cfg.storage_access_key.get_secret_value(),
        secret_key=cfg.storage_secret_key.get_secret_value(),
        region=cfg.storage_region,
    )


def get_object_storage(request: Request) -> ObjectStorage:
    """FastAPI dependency — the process-wide ``ObjectStorage`` built during lifespan startup."""
    storage: ObjectStorage | None = getattr(request.app.state, "object_storage", None)
    if storage is None:  # pragma: no cover - composition-root invariant
        raise RuntimeError("object storage not initialized on app.state")
    return storage
