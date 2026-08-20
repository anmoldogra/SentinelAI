"""Object storage foundation (ADR-0008, guide Part 9).

A provider-neutral, streaming ``ObjectStorage`` port and its S3-compatible (MinIO/S3) adapter — the
blob-persistence layer for evidence. Consumers import ONLY from here: the port, its value objects,
the error taxonomy, the ``s3://`` URI helpers, and the composition functions.

Evidence-specific behaviour (quarantine/scan/promote/WORM, envelope encryption, server-side
integrity hashing) is layered on top by later increments, not here.
"""

from sentinelai.platform.storage.exceptions import (
    BucketNotFound,
    InvalidObjectUri,
    ObjectNotFound,
    StorageAccessDenied,
    StorageError,
    StorageUnavailable,
)
from sentinelai.platform.storage.factory import build_object_storage, get_object_storage
from sentinelai.platform.storage.minio import MinioObjectStorage
from sentinelai.platform.storage.port import CompletedPart, ObjectHead, ObjectStorage
from sentinelai.platform.storage.uri import build_object_uri, parse_object_uri

__all__ = [
    "BucketNotFound",
    "CompletedPart",
    "InvalidObjectUri",
    "MinioObjectStorage",
    "ObjectHead",
    "ObjectNotFound",
    "ObjectStorage",
    "StorageAccessDenied",
    "StorageError",
    "StorageUnavailable",
    "build_object_storage",
    "build_object_uri",
    "get_object_storage",
    "parse_object_uri",
]
