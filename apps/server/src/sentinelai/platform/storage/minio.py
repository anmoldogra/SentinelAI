"""S3-compatible object-storage adapter (MinIO/S3) — ADR-0008, guide Part 9.

Implements :class:`~sentinelai.platform.storage.port.ObjectStorage` over ``aioboto3`` (the client
named by guide Part 9). Works against MinIO on-prem and AWS S3 in cloud via ``endpoint_url`` +
path-style addressing. Uploads stream through a multipart upload, buffering at most one part at a
time — never the whole object.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator, Sequence
from contextlib import contextmanager
from typing import Any

import aioboto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from sentinelai.platform.storage.exceptions import (
    BucketNotFound,
    ObjectNotFound,
    StorageAccessDenied,
    StorageError,
    StorageUnavailable,
)
from sentinelai.platform.storage.port import CompletedPart, ObjectHead

# 8 MiB parts: above S3's 5 MiB minimum for non-final parts, small enough to bound memory.
_PART_SIZE = 8 * 1024 * 1024
# Download chunk size streamed to the caller.
_CHUNK_SIZE = 1024 * 1024
# S3's hard limit for a single-request server-side CopyObject; above it, ranged UploadPartCopy.
_MAX_SINGLE_COPY = 5 * 1024**3
_COPY_PART_SIZE = 1024**3

# S3/MinIO error codes that mean "the object isn't there" vs "the bucket isn't there".
_MISSING_OBJECT_CODES = frozenset({"NoSuchKey", "NoSuchUpload", "NotFound", "404"})
_MISSING_BUCKET_CODES = frozenset({"NoSuchBucket"})
_DENIED_CODES = frozenset({"AccessDenied", "InvalidAccessKeyId", "SignatureDoesNotMatch", "403"})


def _http_status(exc: ClientError) -> int:
    meta = exc.response.get("ResponseMetadata", {}) if isinstance(exc.response, dict) else {}
    status = meta.get("HTTPStatusCode")
    return int(status) if isinstance(status, int) else 0


def _error_code(exc: ClientError) -> str:
    error = exc.response.get("Error", {}) if isinstance(exc.response, dict) else {}
    code = error.get("Code")
    return str(code) if code is not None else ""


def _translate(exc: ClientError, bucket: str, key: str | None = None) -> StorageError:
    """Map a botocore ``ClientError`` onto the provider-neutral storage taxonomy."""
    code = _error_code(exc)
    status = _http_status(exc)
    target = f"{bucket}/{key}" if key is not None else bucket
    if code in _MISSING_BUCKET_CODES:
        return BucketNotFound(bucket)
    if code in _MISSING_OBJECT_CODES or status == 404:
        # A 404 on a keyed operation is a missing object; on a bucket operation, a missing bucket.
        return ObjectNotFound(target) if key is not None else BucketNotFound(bucket)
    if code in _DENIED_CODES or status == 403:
        return StorageAccessDenied(f"access denied for {target}")
    return StorageError(f"storage operation failed for {target} (code={code or status})")


@contextmanager
def _mapped(bucket: str, key: str | None = None) -> Iterator[None]:
    """Translate provider exceptions raised inside the block into ``StorageError`` subclasses.

    Transport-level botocore failures (unreachable endpoint, timeouts) become
    ``StorageUnavailable``; protocol errors go through :func:`_translate`. Nothing botocore-specific
    escapes the port.
    """
    try:
        yield
    except ClientError as exc:
        raise _translate(exc, bucket, key) from exc
    except BotoCoreError as exc:
        raise StorageUnavailable(f"storage endpoint unreachable for {bucket}") from exc


class MinioObjectStorage:
    """``ObjectStorage`` implementation backed by an S3-compatible endpoint (MinIO/S3)."""

    def __init__(
        self,
        session: aioboto3.Session,
        *,
        endpoint_url: str,
        access_key: str,
        secret_key: str,
        region: str = "us-east-1",
    ) -> None:
        self._session = session
        self._endpoint_url = endpoint_url
        self._access_key = access_key
        self._secret_key = secret_key
        self._region = region

    def _client(self) -> Any:
        # A fresh client (and connection) per operation — the pattern in guide Part 9's sketch.
        # Path-style addressing is required for MinIO and works for S3.
        return self._session.client(
            "s3",
            endpoint_url=self._endpoint_url,
            aws_access_key_id=self._access_key,
            aws_secret_access_key=self._secret_key,
            region_name=self._region,
            config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
        )

    async def ensure_bucket(self, bucket: str) -> None:
        with _mapped(bucket):
            async with self._client() as s3:
                try:
                    await s3.head_bucket(Bucket=bucket)
                except ClientError as exc:
                    if _http_status(exc) != 404:
                        raise  # translated by _mapped
                    await s3.create_bucket(Bucket=bucket)

    async def put_stream(
        self,
        bucket: str,
        key: str,
        data: AsyncIterator[bytes],
        *,
        content_type: str | None = None,
    ) -> None:
        extra = {"ContentType": content_type} if content_type else {}
        with _mapped(bucket, key):
            async with self._client() as s3:
                created = await s3.create_multipart_upload(Bucket=bucket, Key=key, **extra)
                upload_id: str = created["UploadId"]
                parts: list[dict[str, Any]] = []
                buffer = bytearray()
                number = 1
                try:
                    async for chunk in data:
                        buffer.extend(chunk)
                        while len(buffer) >= _PART_SIZE:
                            parts.append(
                                await self._flush_part(s3, bucket, key, upload_id, number, buffer)
                            )
                            number += 1
                    # Final part: the remaining bytes, or one empty part if the stream was empty
                    # (S3 requires at least one part to complete the upload).
                    if buffer or not parts:
                        parts.append(
                            await self._flush_part(
                                s3, bucket, key, upload_id, number, buffer, final=True
                            )
                        )
                    await s3.complete_multipart_upload(
                        Bucket=bucket,
                        Key=key,
                        UploadId=upload_id,
                        MultipartUpload={"Parts": parts},
                    )
                except BaseException:
                    # Never leave a half-written multipart upload (and its storage cost) behind.
                    await s3.abort_multipart_upload(Bucket=bucket, Key=key, UploadId=upload_id)
                    raise

    @staticmethod
    async def _flush_part(
        s3: Any,
        bucket: str,
        key: str,
        upload_id: str,
        number: int,
        buffer: bytearray,
        *,
        final: bool = False,
    ) -> dict[str, Any]:
        body = bytes(buffer if final else buffer[:_PART_SIZE])
        if not final:
            del buffer[:_PART_SIZE]
        else:
            buffer.clear()
        result = await s3.upload_part(
            Bucket=bucket, Key=key, UploadId=upload_id, PartNumber=number, Body=body
        )
        return {"PartNumber": number, "ETag": result["ETag"]}

    async def get_stream(self, bucket: str, key: str) -> AsyncIterator[bytes]:
        with _mapped(bucket, key):
            async with self._client() as s3:
                response = await s3.get_object(Bucket=bucket, Key=key)
                body = response["Body"]
                while True:
                    chunk: bytes = await body.read(_CHUNK_SIZE)
                    if not chunk:
                        break
                    yield chunk

    async def head(self, bucket: str, key: str) -> ObjectHead:
        with _mapped(bucket, key):
            async with self._client() as s3:
                meta = await s3.head_object(Bucket=bucket, Key=key)
        size: int = meta["ContentLength"]
        etag: str = str(meta["ETag"]).strip('"')
        return ObjectHead(
            size=size,
            etag=etag,
            content_type=meta.get("ContentType"),
            last_modified=meta.get("LastModified"),
        )

    async def exists(self, bucket: str, key: str) -> bool:
        try:
            await self.head(bucket, key)
        except ObjectNotFound:
            return False
        return True

    async def copy_object(
        self, source_bucket: str, source_key: str, dest_bucket: str, dest_key: str
    ) -> None:
        # `head` raises ObjectNotFound for a missing source and gives the size that decides
        # whether S3's single-request CopyObject (5 GiB hard limit) is usable. Multi-GB forensic
        # images exceed it, so larger objects copy via ranged UploadPartCopy — still entirely
        # server-side; no byte passes through this process on either path.
        head = await self.head(source_bucket, source_key)
        source = {"Bucket": source_bucket, "Key": source_key}
        with _mapped(dest_bucket, dest_key):
            async with self._client() as s3:
                if head.size <= _MAX_SINGLE_COPY:
                    await s3.copy_object(Bucket=dest_bucket, Key=dest_key, CopySource=source)
                    return
                created = await s3.create_multipart_upload(Bucket=dest_bucket, Key=dest_key)
                upload_id: str = created["UploadId"]
                try:
                    parts: list[dict[str, Any]] = []
                    number = 1
                    offset = 0
                    while offset < head.size:
                        last = min(offset + _COPY_PART_SIZE, head.size) - 1
                        result = await s3.upload_part_copy(
                            Bucket=dest_bucket,
                            Key=dest_key,
                            UploadId=upload_id,
                            PartNumber=number,
                            CopySource=source,
                            CopySourceRange=f"bytes={offset}-{last}",
                        )
                        parts.append(
                            {"PartNumber": number, "ETag": result["CopyPartResult"]["ETag"]}
                        )
                        offset = last + 1
                        number += 1
                    await s3.complete_multipart_upload(
                        Bucket=dest_bucket,
                        Key=dest_key,
                        UploadId=upload_id,
                        MultipartUpload={"Parts": parts},
                    )
                except BaseException:
                    await s3.abort_multipart_upload(
                        Bucket=dest_bucket, Key=dest_key, UploadId=upload_id
                    )
                    raise

    async def delete(self, bucket: str, key: str) -> None:
        with _mapped(bucket, key):
            async with self._client() as s3:
                await s3.delete_object(Bucket=bucket, Key=key)  # S3 delete is idempotent

    async def create_multipart_upload(
        self, bucket: str, key: str, *, content_type: str | None = None
    ) -> str:
        extra = {"ContentType": content_type} if content_type else {}
        with _mapped(bucket, key):
            async with self._client() as s3:
                created = await s3.create_multipart_upload(Bucket=bucket, Key=key, **extra)
        upload_id: str = created["UploadId"]
        return upload_id

    async def upload_part(
        self, bucket: str, key: str, upload_id: str, part_number: int, data: bytes
    ) -> CompletedPart:
        with _mapped(bucket, key):
            async with self._client() as s3:
                result = await s3.upload_part(
                    Bucket=bucket, Key=key, UploadId=upload_id, PartNumber=part_number, Body=data
                )
        return CompletedPart(part_number=part_number, etag=str(result["ETag"]))

    async def complete_multipart_upload(
        self, bucket: str, key: str, upload_id: str, parts: Sequence[CompletedPart]
    ) -> None:
        payload = [{"PartNumber": p.part_number, "ETag": p.etag} for p in parts]
        with _mapped(bucket, key):
            async with self._client() as s3:
                await s3.complete_multipart_upload(
                    Bucket=bucket, Key=key, UploadId=upload_id, MultipartUpload={"Parts": payload}
                )

    async def abort_multipart_upload(self, bucket: str, key: str, upload_id: str) -> None:
        with _mapped(bucket, key):
            async with self._client() as s3:
                await s3.abort_multipart_upload(Bucket=bucket, Key=key, UploadId=upload_id)

    async def presigned_upload_url(self, bucket: str, key: str, *, expires_in: int = 900) -> str:
        # Presigning is a local signing operation — it neither contacts the endpoint nor proves the
        # object exists. The URL is a bearer credential: never log it (security-architecture §35).
        with _mapped(bucket, key):
            async with self._client() as s3:
                url: str = await s3.generate_presigned_url(
                    "put_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=expires_in
                )
        return url

    async def presigned_download_url(self, bucket: str, key: str, *, expires_in: int = 900) -> str:
        with _mapped(bucket, key):
            async with self._client() as s3:
                url: str = await s3.generate_presigned_url(
                    "get_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=expires_in
                )
        return url
