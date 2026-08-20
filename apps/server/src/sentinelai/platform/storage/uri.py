"""Object URI helpers — the ``s3://bucket/key`` form used by CEM ``payload_ref`` (CEM §5, line 46;
example at CEM §14). Provider-neutral in shape: the scheme is a stable pointer format, not a
statement that the backend is AWS S3 (it is MinIO on-prem).

Kept out of ``port.py`` on purpose — the port is the behavioural contract; this is the addressing
convention that maps a stored evidence pointer onto a ``(bucket, key)`` pair.
"""

from __future__ import annotations

from sentinelai.platform.storage.exceptions import InvalidObjectUri

_SCHEME = "s3://"


def build_object_uri(bucket: str, key: str) -> str:
    """Return the canonical ``s3://bucket/key`` URI for a stored object."""
    return f"{_SCHEME}{bucket}/{key}"


def parse_object_uri(uri: str) -> tuple[str, str]:
    """Split ``s3://bucket/key`` into ``(bucket, key)``.

    Raises :class:`InvalidObjectUri` for anything that is not a well-formed reference with a
    non-empty bucket and key.
    """
    if not uri.startswith(_SCHEME):
        raise InvalidObjectUri(f"object URI must start with {_SCHEME!r}")
    bucket, separator, key = uri[len(_SCHEME) :].partition("/")
    if not separator or not bucket or not key:
        raise InvalidObjectUri("object URI must be of the form s3://<bucket>/<key>")
    return bucket, key


__all__ = ["build_object_uri", "parse_object_uri"]
