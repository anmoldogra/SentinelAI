"""Unit tests for the S3-compatible adapter itself (ADR-0008).

Drives ``MinioObjectStorage`` against a stub S3 client, so the behaviour that only the real adapter
has — multipart chunking of a stream, abort-on-failure, botocore error translation, region/endpoint
wiring — is covered deterministically, with no MinIO and no network. The same adapter is exercised
end-to-end against a real MinIO by ``tests/integration/test_object_storage_minio.py``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError

from sentinelai.platform.storage import minio as minio_module
from sentinelai.platform.storage.exceptions import (
    BucketNotFound,
    ObjectNotFound,
    StorageAccessDenied,
    StorageError,
    StorageUnavailable,
)
from sentinelai.platform.storage.minio import MinioObjectStorage


def _client_error(code: str, status: int, operation: str = "HeadObject") -> ClientError:
    return ClientError(
        {"Error": {"Code": code}, "ResponseMetadata": {"HTTPStatusCode": status}}, operation
    )


class _Body:
    """Stand-in for botocore's streaming response body."""

    def __init__(self, blob: bytes) -> None:
        self._blob = blob
        self._pos = 0

    async def read(self, size: int) -> bytes:
        chunk = self._blob[self._pos : self._pos + size]
        self._pos += len(chunk)
        return chunk


class _FakeS3:
    """Minimal async stub of the aioboto3 S3 client surface the adapter uses."""

    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}
        self.buckets: set[str] = set()
        self.uploads: dict[str, list[tuple[int, bytes]]] = {}
        self.calls: list[str] = []
        self.aborted: list[str] = []
        self.raise_on: dict[str, Exception] = {}

    async def __aenter__(self) -> _FakeS3:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    def _maybe_raise(self, op: str) -> None:
        self.calls.append(op)
        if op in self.raise_on:
            raise self.raise_on[op]

    async def head_bucket(self, *, Bucket: str) -> dict[str, Any]:
        self._maybe_raise("head_bucket")
        if Bucket not in self.buckets:
            raise _client_error("404", 404, "HeadBucket")
        return {}

    async def create_bucket(self, *, Bucket: str) -> dict[str, Any]:
        self._maybe_raise("create_bucket")
        self.buckets.add(Bucket)
        return {}

    async def create_multipart_upload(self, *, Bucket: str, Key: str, **kw: Any) -> dict[str, Any]:
        self._maybe_raise("create_multipart_upload")
        upload_id = f"u{len(self.uploads) + 1}"
        self.uploads[upload_id] = []
        return {"UploadId": upload_id}

    async def upload_part(
        self, *, Bucket: str, Key: str, UploadId: str, PartNumber: int, Body: bytes
    ) -> dict[str, Any]:
        self._maybe_raise("upload_part")
        self.uploads[UploadId].append((PartNumber, Body))
        return {"ETag": f'"etag-{PartNumber}"'}

    async def complete_multipart_upload(
        self, *, Bucket: str, Key: str, UploadId: str, MultipartUpload: dict[str, Any]
    ) -> dict[str, Any]:
        self._maybe_raise("complete_multipart_upload")
        ordered = sorted(self.uploads.pop(UploadId), key=lambda p: p[0])
        self.objects[(Bucket, Key)] = b"".join(body for _, body in ordered)
        return {}

    async def abort_multipart_upload(
        self, *, Bucket: str, Key: str, UploadId: str
    ) -> dict[str, Any]:
        self.aborted.append(UploadId)
        self.uploads.pop(UploadId, None)
        return {}

    async def head_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:
        self._maybe_raise("head_object")
        if (Bucket, Key) not in self.objects:
            raise _client_error("NoSuchKey", 404)
        blob = self.objects[(Bucket, Key)]
        return {"ContentLength": len(blob), "ETag": '"abc"', "ContentType": "application/pdf"}

    async def get_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:
        self._maybe_raise("get_object")
        if (Bucket, Key) not in self.objects:
            raise _client_error("NoSuchKey", 404, "GetObject")
        return {"Body": _Body(self.objects[(Bucket, Key)])}

    async def delete_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:
        self._maybe_raise("delete_object")
        self.objects.pop((Bucket, Key), None)
        return {}

    async def copy_object(
        self, *, Bucket: str, Key: str, CopySource: dict[str, str]
    ) -> dict[str, Any]:
        self._maybe_raise("copy_object")
        self.objects[(Bucket, Key)] = self.objects[(CopySource["Bucket"], CopySource["Key"])]
        return {}

    async def upload_part_copy(
        self,
        *,
        Bucket: str,
        Key: str,
        UploadId: str,
        PartNumber: int,
        CopySource: dict[str, str],
        CopySourceRange: str,
    ) -> dict[str, Any]:
        self._maybe_raise("upload_part_copy")
        source = self.objects[(CopySource["Bucket"], CopySource["Key"])]
        span = CopySourceRange.removeprefix("bytes=")
        start, end = (int(part) for part in span.split("-"))
        self.uploads[UploadId].append((PartNumber, source[start : end + 1]))
        return {"CopyPartResult": {"ETag": f'"copy-etag-{PartNumber}"'}}

    async def generate_presigned_url(
        self, operation: str, *, Params: dict[str, str], ExpiresIn: int
    ) -> str:
        self._maybe_raise("generate_presigned_url")
        return f"https://s3.local/{Params['Bucket']}/{Params['Key']}?op={operation}&e={ExpiresIn}"


class _FakeSession:
    def __init__(self, client: _FakeS3) -> None:
        self._client = client
        self.client_kwargs: dict[str, Any] = {}

    def client(self, service: str, **kwargs: Any) -> _FakeS3:
        self.client_kwargs = {"service": service, **kwargs}
        return self._client


def _adapter(client: _FakeS3) -> tuple[MinioObjectStorage, _FakeSession]:
    session = _FakeSession(client)
    storage = MinioObjectStorage(
        session,  # type: ignore[arg-type]
        endpoint_url="http://minio.local:9000",
        access_key="ak",
        secret_key="sk",
        region="eu-west-1",
    )
    return storage, session


async def _stream(*chunks: bytes) -> AsyncIterator[bytes]:
    for chunk in chunks:
        yield chunk


# --- configuration wiring ---------------------------------------------------


async def test_client_is_built_with_configured_endpoint_and_region() -> None:
    storage, session = _adapter(_FakeS3())
    await storage.ensure_bucket("b")
    assert session.client_kwargs["service"] == "s3"
    assert session.client_kwargs["endpoint_url"] == "http://minio.local:9000"
    assert session.client_kwargs["region_name"] == "eu-west-1"
    # Path-style addressing is what makes the same adapter work against MinIO.
    assert session.client_kwargs["config"].s3["addressing_style"] == "path"


async def test_ensure_bucket_creates_only_when_absent() -> None:
    client = _FakeS3()
    storage, _ = _adapter(client)
    await storage.ensure_bucket("b")
    assert "create_bucket" in client.calls
    client.calls.clear()
    await storage.ensure_bucket("b")  # now exists
    assert "create_bucket" not in client.calls


# --- streaming / multipart --------------------------------------------------


async def test_put_stream_splits_a_large_stream_into_multiple_parts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(minio_module, "_PART_SIZE", 1024)
    client = _FakeS3()
    storage, _ = _adapter(client)
    payload = b"x" * 5000  # 4 full 1 KiB parts + a 904-byte remainder

    await storage.put_stream("b", "big", _stream(payload[:3000], payload[3000:]))

    assert client.objects[("b", "big")] == payload
    assert client.calls.count("upload_part") == 5
    assert client.calls.count("complete_multipart_upload") == 1


async def test_put_stream_of_an_empty_stream_writes_one_empty_part() -> None:
    client = _FakeS3()
    storage, _ = _adapter(client)
    await storage.put_stream("b", "empty", _stream())
    assert client.objects[("b", "empty")] == b""
    assert client.calls.count("upload_part") == 1


async def test_put_stream_aborts_the_upload_when_the_source_fails() -> None:
    client = _FakeS3()
    storage, _ = _adapter(client)

    async def _exploding() -> AsyncIterator[bytes]:
        yield b"partial"
        raise RuntimeError("source died")

    with pytest.raises(RuntimeError, match="source died"):
        await storage.put_stream("b", "k", _exploding())
    assert client.aborted == ["u1"]  # no orphaned multipart upload left behind
    assert ("b", "k") not in client.objects


async def test_get_stream_yields_chunks_lazily(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(minio_module, "_CHUNK_SIZE", 8)
    client = _FakeS3()
    client.objects[("b", "k")] = b"0123456789abcdefgh"
    storage, _ = _adapter(client)
    chunks = [chunk async for chunk in storage.get_stream("b", "k")]
    assert chunks == [b"01234567", b"89abcdef", b"gh"]


async def test_explicit_multipart_round_trip() -> None:
    client = _FakeS3()
    storage, _ = _adapter(client)
    upload_id = await storage.create_multipart_upload("b", "k", content_type="application/pdf")
    p1 = await storage.upload_part("b", "k", upload_id, 1, b"AAA")
    p2 = await storage.upload_part("b", "k", upload_id, 2, b"BB")
    assert (p1.part_number, p2.part_number) == (1, 2)
    await storage.complete_multipart_upload("b", "k", upload_id, [p1, p2])
    assert client.objects[("b", "k")] == b"AAABB"


async def test_abort_multipart_upload_discards_parts() -> None:
    client = _FakeS3()
    storage, _ = _adapter(client)
    upload_id = await storage.create_multipart_upload("b", "k")
    await storage.upload_part("b", "k", upload_id, 1, b"AAA")
    await storage.abort_multipart_upload("b", "k", upload_id)
    assert upload_id not in client.uploads


# --- metadata / existence ---------------------------------------------------


async def test_head_maps_metadata_and_strips_etag_quotes() -> None:
    client = _FakeS3()
    client.objects[("b", "k")] = b"12345"
    storage, _ = _adapter(client)
    head = await storage.head("b", "k")
    assert (head.size, head.etag, head.content_type) == (5, "abc", "application/pdf")


async def test_exists_reflects_presence_without_reading_bytes() -> None:
    client = _FakeS3()
    client.objects[("b", "there")] = b"x"
    storage, _ = _adapter(client)
    assert await storage.exists("b", "there") is True
    assert await storage.exists("b", "absent") is False
    assert "get_object" not in client.calls


# --- server-side copy (ADR-0008 §2 promotion) -------------------------------


async def test_a_small_object_copies_in_a_single_request() -> None:
    client = _FakeS3()
    client.objects[("quarantine", "k")] = b"promote-me"
    storage, _ = _adapter(client)
    await storage.copy_object("quarantine", "k", "evidence", "k")
    assert client.objects[("evidence", "k")] == b"promote-me"
    assert client.calls.count("copy_object") == 1
    assert "upload_part_copy" not in client.calls  # no multipart for a small object


async def test_a_copy_never_removes_the_source() -> None:
    """Promotion is copy-then-delete; the adapter itself must not delete."""
    client = _FakeS3()
    client.objects[("quarantine", "k")] = b"bytes"
    storage, _ = _adapter(client)
    await storage.copy_object("quarantine", "k", "evidence", "k")
    assert ("quarantine", "k") in client.objects


async def test_copying_a_missing_source_raises_object_not_found() -> None:
    storage, _ = _adapter(_FakeS3())
    with pytest.raises(ObjectNotFound):
        await storage.copy_object("quarantine", "absent", "evidence", "absent")


async def test_an_object_above_the_single_copy_limit_uses_ranged_multipart_copy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """S3 caps a single CopyObject at 5 GiB; a multi-GB forensic image must still promote."""
    monkeypatch.setattr(minio_module, "_MAX_SINGLE_COPY", 1000)
    monkeypatch.setattr(minio_module, "_COPY_PART_SIZE", 400)
    payload = bytes(range(256)) * 20  # 5120 bytes -> 13 ranged parts
    client = _FakeS3()
    client.objects[("quarantine", "big")] = payload
    storage, _ = _adapter(client)

    await storage.copy_object("quarantine", "big", "evidence", "big")

    assert client.objects[("evidence", "big")] == payload  # reassembled byte-for-byte
    assert client.calls.count("upload_part_copy") == 13
    assert "copy_object" not in client.calls  # the single-request path was not used


async def test_a_failed_ranged_copy_aborts_the_multipart_upload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(minio_module, "_MAX_SINGLE_COPY", 10)
    monkeypatch.setattr(minio_module, "_COPY_PART_SIZE", 10)
    client = _FakeS3()
    client.objects[("quarantine", "big")] = b"x" * 100
    client.raise_on["upload_part_copy"] = _client_error("InternalError", 500, "UploadPartCopy")
    storage, _ = _adapter(client)

    with pytest.raises(StorageError):
        await storage.copy_object("quarantine", "big", "evidence", "big")
    assert client.aborted == ["u1"]  # no orphaned multipart upload left behind


# --- presigned URLs ---------------------------------------------------------


async def test_presigned_urls_carry_the_requested_operation_and_ttl() -> None:
    client = _FakeS3()
    storage, _ = _adapter(client)
    put_url = await storage.presigned_upload_url("b", "k", expires_in=300)
    get_url = await storage.presigned_download_url("b", "k", expires_in=60)
    assert "op=put_object" in put_url and "e=300" in put_url
    assert "op=get_object" in get_url and "e=60" in get_url


# --- error translation ------------------------------------------------------


async def test_missing_object_raises_object_not_found() -> None:
    storage, _ = _adapter(_FakeS3())
    with pytest.raises(ObjectNotFound):
        await storage.head("b", "absent")
    with pytest.raises(KeyError):  # ObjectNotFound is also a KeyError
        await storage.head("b", "absent")


async def test_missing_object_on_get_stream_raises_object_not_found() -> None:
    storage, _ = _adapter(_FakeS3())
    with pytest.raises(ObjectNotFound):
        _ = [chunk async for chunk in storage.get_stream("b", "absent")]


async def test_missing_bucket_raises_bucket_not_found() -> None:
    client = _FakeS3()
    client.raise_on["head_object"] = _client_error("NoSuchBucket", 404)
    storage, _ = _adapter(client)
    with pytest.raises(BucketNotFound):
        await storage.head("b", "k")


async def test_denied_credentials_raise_access_denied() -> None:
    client = _FakeS3()
    client.raise_on["head_object"] = _client_error("AccessDenied", 403)
    storage, _ = _adapter(client)
    with pytest.raises(StorageAccessDenied):
        await storage.head("b", "k")


async def test_unreachable_endpoint_raises_storage_unavailable() -> None:
    client = _FakeS3()
    client.raise_on["head_bucket"] = EndpointConnectionError(endpoint_url="http://minio.local:9000")
    storage, _ = _adapter(client)
    with pytest.raises(StorageUnavailable):
        await storage.ensure_bucket("b")


async def test_unmapped_client_error_raises_generic_storage_error() -> None:
    client = _FakeS3()
    client.raise_on["delete_object"] = _client_error("InternalError", 500, "DeleteObject")
    storage, _ = _adapter(client)
    with pytest.raises(StorageError) as caught:
        await storage.delete("b", "k")
    assert not isinstance(caught.value, ObjectNotFound | StorageAccessDenied)


async def test_no_botocore_exception_escapes_the_port() -> None:
    """Every translated failure is a StorageError — callers never import botocore to handle one."""
    client = _FakeS3()
    client.raise_on["head_object"] = _client_error("SignatureDoesNotMatch", 403)
    storage, _ = _adapter(client)
    with pytest.raises(StorageError) as caught:
        await storage.head("b", "k")
    assert not isinstance(caught.value, ClientError)
