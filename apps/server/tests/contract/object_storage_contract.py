"""Shared ``ObjectStorage`` contract (ADR-0008).

A single behavioural spec exercised against BOTH the in-memory fake (unit) and the real MinIO
adapter (integration), so any implementation must satisfy identical semantics. Not a test module
itself (no ``test_`` prefix) — imported by the unit and integration tests.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from sentinelai.platform.storage.port import ObjectStorage

_FIVE_MIB = 5 * 1024 * 1024


async def _stream(*chunks: bytes) -> AsyncIterator[bytes]:
    for chunk in chunks:
        yield chunk


async def _drain(source: AsyncIterator[bytes]) -> bytes:
    return b"".join([chunk async for chunk in source])


async def check_object_storage(storage: ObjectStorage, *, bucket: str) -> None:
    """Assert the full ObjectStorage contract against ``storage`` in ``bucket``."""
    await storage.ensure_bucket(bucket)
    await storage.ensure_bucket(bucket)  # idempotent

    # put_stream (chunked) → head → get_stream round-trip
    payload = b"hello-evidence-" * 1000
    await storage.put_stream(
        bucket, "obj/a", _stream(payload[:600], payload[600:]), content_type="text/plain"
    )
    head = await storage.head(bucket, "obj/a")
    assert head.size == len(payload)
    assert await _drain(storage.get_stream(bucket, "obj/a")) == payload

    # presigned URLs are non-empty http(s) URLs
    assert (await storage.presigned_upload_url(bucket, "obj/a")).startswith("http")
    assert (await storage.presigned_download_url(bucket, "obj/a")).startswith("http")

    # explicit multipart API round-trip (first part >= 5 MiB per S3 rules; last part small)
    upload_id = await storage.create_multipart_upload(bucket, "obj/b")
    part1 = await storage.upload_part(bucket, "obj/b", upload_id, 1, b"A" * _FIVE_MIB)
    part2 = await storage.upload_part(bucket, "obj/b", upload_id, 2, b"B" * 17)
    await storage.complete_multipart_upload(bucket, "obj/b", upload_id, [part1, part2])
    assert await _drain(storage.get_stream(bucket, "obj/b")) == b"A" * _FIVE_MIB + b"B" * 17

    # delete → head raises KeyError; delete is idempotent
    await storage.delete(bucket, "obj/a")
    await storage.delete(bucket, "obj/a")  # missing key is not an error
    with pytest.raises(KeyError):
        await storage.head(bucket, "obj/a")
    with pytest.raises(KeyError):
        await _drain(storage.get_stream(bucket, "obj/a"))

    await storage.delete(bucket, "obj/b")
