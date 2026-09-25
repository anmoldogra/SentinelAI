"""Object-storage port — the provider-neutral blob abstraction (ADR-0008, guide Part 9).

Consumers depend on the ``ObjectStorage`` Protocol, never on a concrete S3/MinIO client, so a
deployment can move MinIO ↔ S3 by configuration alone. Everything here is **streaming-first**: an
object is never fully buffered in memory — uploads take an async byte stream (chunked internally as
a multipart upload), downloads yield an async byte stream.

Failures surface as the provider-neutral taxonomy in ``storage/exceptions.py`` (``StorageError`` and
friends) — never as a botocore exception, so no consumer has to import an S3 client to handle one.

This foundation deliberately contains only generic blob operations. The evidence-specific flow
(quarantine → scan → promote → WORM, envelope encryption, integrity hashing) is layered *on top* of
this port by later increments (ADR-0008 §2-5, ADR-0009, ADR-0003) — not here.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True, slots=True)
class ObjectHead:
    """Metadata for a stored object (the result of a ``head`` request)."""

    size: int
    etag: str
    content_type: str | None
    last_modified: datetime | None


@dataclass(frozen=True, slots=True)
class CompletedPart:
    """One finished part of a multipart upload — the ``part_number`` + its ``etag``."""

    part_number: int
    etag: str


class ObjectStorage(Protocol):
    """Provider-neutral, streaming object storage. Implemented by ``MinioObjectStorage``.

    All methods are async; none buffers a whole object. Keys are opaque strings; buckets are
    created explicitly via :meth:`ensure_bucket`.
    """

    async def ensure_bucket(self, bucket: str) -> None:
        """Create ``bucket`` if it does not already exist (idempotent)."""
        ...

    async def ensure_worm_bucket(self, bucket: str) -> None:
        """Create ``bucket`` with S3 Object Lock enabled, if it does not exist.

        Separate from :meth:`ensure_bucket` because Object Lock **cannot be turned on after
        creation** — it is fixed at create time. A bucket made by the ordinary path can never
        become a WORM bucket, so the two are different operations rather than a flag.
        """
        ...

    async def put_immutable(
        self,
        bucket: str,
        key: str,
        data: bytes,
        *,
        retain_until: datetime,
        content_type: str | None = None,
    ) -> None:
        """Write an object that cannot be modified or deleted until ``retain_until``.

        Uses Object Lock in **COMPLIANCE** mode, not GOVERNANCE: governance retention can be
        bypassed by a principal holding ``s3:BypassGovernanceRetention``, which is exactly the
        privileged insider ADR-0003 §3 exists to defend against. An anchor a DBA-equivalent can
        delete anchors nothing.

        Takes ``bytes`` rather than a stream: the objects written this way are anchors — a few
        hundred bytes — and a single atomic PUT is both simpler and the only form that carries
        the lock headers.
        """
        ...

    async def put_stream(
        self,
        bucket: str,
        key: str,
        data: AsyncIterator[bytes],
        *,
        content_type: str | None = None,
    ) -> None:
        """Store ``data`` at ``bucket/key``, streaming it in bounded parts (never fully buffered).

        The stream is consumed lazily and uploaded as a multipart upload.
        """
        ...

    def get_stream(self, bucket: str, key: str) -> AsyncIterator[bytes]:
        """Return an async byte-stream of ``bucket/key`` (consumed lazily, never fully buffered).

        Raises ``ObjectNotFound`` when the stream is first consumed, not when it is created.
        """
        ...

    async def head(self, bucket: str, key: str) -> ObjectHead:
        """Return object metadata; raises ``ObjectNotFound`` if the object does not exist."""
        ...

    async def exists(self, bucket: str, key: str) -> bool:
        """Return whether ``bucket/key`` exists, without transferring the object's bytes."""
        ...

    async def copy_object(
        self, source_bucket: str, source_key: str, dest_bucket: str, dest_key: str
    ) -> None:
        """Copy an object **server-side** — the bytes never transit this process.

        Used to promote an object between buckets (ADR-0008 §2). Raises ``ObjectNotFound`` if the
        source does not exist. Overwrites the destination if present.
        """
        ...

    async def delete(self, bucket: str, key: str) -> None:
        """Delete ``bucket/key`` (idempotent — deleting a missing key is not an error)."""
        ...

    # -- explicit multipart API (for callers streaming very large objects) --
    async def create_multipart_upload(
        self, bucket: str, key: str, *, content_type: str | None = None
    ) -> str:
        """Begin a multipart upload; returns the ``upload_id``."""
        ...

    async def upload_part(
        self, bucket: str, key: str, upload_id: str, part_number: int, data: bytes
    ) -> CompletedPart:
        """Upload one part (1-indexed ``part_number``); returns the part's etag for completion."""
        ...

    async def complete_multipart_upload(
        self, bucket: str, key: str, upload_id: str, parts: Sequence[CompletedPart]
    ) -> None:
        """Finalise a multipart upload from its completed parts (in ``part_number`` order)."""
        ...

    async def abort_multipart_upload(self, bucket: str, key: str, upload_id: str) -> None:
        """Abort an in-flight multipart upload, discarding any uploaded parts."""
        ...

    # -- presigned URLs (client uploads/downloads directly, bypassing the app) --
    async def presigned_upload_url(self, bucket: str, key: str, *, expires_in: int = 900) -> str:
        """Return a time-limited presigned PUT URL for a direct client upload (guide Part 9)."""
        ...

    async def presigned_download_url(self, bucket: str, key: str, *, expires_in: int = 900) -> str:
        """Return a time-limited presigned GET URL for a direct client download (guide Part 9)."""
        ...
