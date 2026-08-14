"""Object storage foundation (ADR-0008, guide Part 9).

A provider-neutral, streaming ``ObjectStorage`` port and its S3-compatible (MinIO/S3) adapter — the
blob-persistence layer for evidence. Evidence-specific behaviour (quarantine/scan/promote/WORM,
envelope encryption, integrity hashing) is layered on top by later increments, not here.
"""

from sentinelai.platform.storage.factory import build_object_storage
from sentinelai.platform.storage.minio import MinioObjectStorage
from sentinelai.platform.storage.port import CompletedPart, ObjectHead, ObjectStorage

__all__ = [
    "CompletedPart",
    "MinioObjectStorage",
    "ObjectHead",
    "ObjectStorage",
    "build_object_storage",
]
