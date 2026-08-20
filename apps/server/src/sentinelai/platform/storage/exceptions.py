"""Object-storage error taxonomy — ADR-0008.

Infrastructure errors, mirroring ``platform.crypto``'s taxonomy (ADR-0009). Their job is to keep
provider-specific exception types (``botocore.exceptions.ClientError``) from crossing the
``ObjectStorage`` port: application code handles a storage failure without importing boto3, which
is what makes the MinIO ↔ S3 swap a configuration change.

They never carry credentials, presigned URLs, or object bytes.
"""

from __future__ import annotations


class StorageError(Exception):
    """Base for all object-storage errors."""


class ObjectNotFound(StorageError, KeyError):
    """The requested object does not exist.

    Deliberately also a ``KeyError``: the port documents missing-object lookups as raising
    ``KeyError``, and callers that only care about absence keep working unchanged, while callers
    that want the storage taxonomy can catch ``StorageError``.
    """


class BucketNotFound(StorageError):
    """The target bucket does not exist (and was not created by ``ensure_bucket``)."""


class StorageAccessDenied(StorageError):
    """The configured credentials are not authorized for the requested operation."""


class StorageUnavailable(StorageError):
    """The storage endpoint is unreachable, timed out, or failed at the transport layer."""


class InvalidObjectUri(StorageError):
    """A stored object URI is not a parseable ``s3://bucket/key`` reference."""


__all__ = [
    "BucketNotFound",
    "InvalidObjectUri",
    "ObjectNotFound",
    "StorageAccessDenied",
    "StorageError",
    "StorageUnavailable",
]
