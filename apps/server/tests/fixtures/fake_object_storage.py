"""In-memory ``ObjectStorage`` fake for unit/contract tests (no network).

Satisfies the same port as ``MinioObjectStorage`` so the contract suite runs against both. It is a
faithful behavioural stand-in — not a streaming one (a fake may buffer); streaming-without-buffering
is the real adapter's concern, verified by the MinIO integration test.
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator, Sequence
from uuid import uuid4

from sentinelai.platform.storage.exceptions import ObjectNotFound
from sentinelai.platform.storage.port import CompletedPart, ObjectHead


class FakeObjectStorage:
    """A dict-backed ``ObjectStorage`` implementation for tests."""

    def __init__(self) -> None:
        self._objects: dict[tuple[str, str], bytes] = {}
        self._content_types: dict[tuple[str, str], str | None] = {}
        self._buckets: set[str] = set()
        self._multipart: dict[str, tuple[str, str, dict[int, bytes]]] = {}

    async def ensure_bucket(self, bucket: str) -> None:
        self._buckets.add(bucket)

    async def put_stream(
        self, bucket: str, key: str, data: AsyncIterator[bytes], *, content_type: str | None = None
    ) -> None:
        buffer = bytearray()
        async for chunk in data:
            buffer.extend(chunk)
        self._objects[(bucket, key)] = bytes(buffer)
        self._content_types[(bucket, key)] = content_type

    async def get_stream(self, bucket: str, key: str) -> AsyncIterator[bytes]:
        if (bucket, key) not in self._objects:
            raise ObjectNotFound(f"{bucket}/{key}")
        blob = self._objects[(bucket, key)]
        for i in range(0, max(len(blob), 1), 1024):
            yield blob[i : i + 1024]

    async def head(self, bucket: str, key: str) -> ObjectHead:
        if (bucket, key) not in self._objects:
            raise ObjectNotFound(f"{bucket}/{key}")
        blob = self._objects[(bucket, key)]
        return ObjectHead(
            size=len(blob),
            etag=hashlib.md5(blob).hexdigest(),
            content_type=self._content_types.get((bucket, key)),
            last_modified=None,
        )

    async def exists(self, bucket: str, key: str) -> bool:
        return (bucket, key) in self._objects

    async def copy_object(
        self, source_bucket: str, source_key: str, dest_bucket: str, dest_key: str
    ) -> None:
        if (source_bucket, source_key) not in self._objects:
            raise ObjectNotFound(f"{source_bucket}/{source_key}")
        self._objects[(dest_bucket, dest_key)] = self._objects[(source_bucket, source_key)]
        self._content_types[(dest_bucket, dest_key)] = self._content_types.get(
            (source_bucket, source_key)
        )

    async def delete(self, bucket: str, key: str) -> None:
        self._objects.pop((bucket, key), None)
        self._content_types.pop((bucket, key), None)

    async def create_multipart_upload(
        self, bucket: str, key: str, *, content_type: str | None = None
    ) -> str:
        upload_id = uuid4().hex
        self._multipart[upload_id] = (bucket, key, {})
        self._content_types[(bucket, key)] = content_type
        return upload_id

    async def upload_part(
        self, bucket: str, key: str, upload_id: str, part_number: int, data: bytes
    ) -> CompletedPart:
        self._multipart[upload_id][2][part_number] = data
        return CompletedPart(part_number=part_number, etag=hashlib.md5(data).hexdigest())

    async def complete_multipart_upload(
        self, bucket: str, key: str, upload_id: str, parts: Sequence[CompletedPart]
    ) -> None:
        _, _, uploaded = self._multipart.pop(upload_id)
        assembled = b"".join(uploaded[p.part_number] for p in parts)
        self._objects[(bucket, key)] = assembled

    async def abort_multipart_upload(self, bucket: str, key: str, upload_id: str) -> None:
        self._multipart.pop(upload_id, None)

    async def presigned_upload_url(self, bucket: str, key: str, *, expires_in: int = 900) -> str:
        return f"https://fake.local/{bucket}/{key}?method=PUT&expires_in={expires_in}"

    async def presigned_download_url(self, bucket: str, key: str, *, expires_in: int = 900) -> str:
        return f"https://fake.local/{bucket}/{key}?method=GET&expires_in={expires_in}"
