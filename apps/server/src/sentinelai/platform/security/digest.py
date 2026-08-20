"""Streaming payload digest — ADR-0008 §3, ADR-0003 §4.

Computes an evidence payload's integrity digest by consuming an **async byte stream**, so a
multi-GB forensic image is hashed without ever being held in memory. Pairs with
``platform.storage``'s ``ObjectStorage.get_stream``, but takes a plain ``AsyncIterator[bytes]`` and
knows nothing about object storage — the storage adapter stays free of integrity semantics and this
primitive stays testable without any store.

Distinct from ``security.hashing`` (argon2id password KDF): this is a fast content digest over
evidence bytes, deliberately *not* a slow KDF.

Algorithms are restricted to the set the Canonical Evidence Model permits as a primary integrity
hash (CEM §13: SHA-256, SHA-3-256, SHA-512). Comparison is constant-time.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import AsyncIterator
from dataclasses import dataclass

# CEM §13's allowed primary integrity algorithms → hashlib constructor names.
_ALGORITHMS: dict[str, str] = {
    "SHA-256": "sha256",
    "SHA-3-256": "sha3_256",
    "SHA-512": "sha512",
}

_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")


class UnsupportedDigestAlgorithm(ValueError):
    """The requested algorithm is not an allowed primary integrity algorithm (CEM §13)."""


@dataclass(frozen=True, slots=True)
class StreamDigest:
    """The result of digesting a stream: the algorithm, its hex digest, and the bytes consumed."""

    algorithm: str
    hex_digest: str
    size_bytes: int


def is_valid_digest(value: str, algorithm: str = "SHA-256") -> bool:
    """Return whether ``value`` is a well-formed hex digest of the right width for ``algorithm``."""
    if algorithm not in _ALGORITHMS:
        return False
    expected_width = hashlib.new(_ALGORITHMS[algorithm]).digest_size * 2
    return len(value) == expected_width and all(char in _HEX_DIGITS for char in value)


async def compute_stream_digest(
    stream: AsyncIterator[bytes], algorithm: str = "SHA-256"
) -> StreamDigest:
    """Digest ``stream`` incrementally, holding only one chunk at a time.

    Raises :class:`UnsupportedDigestAlgorithm` for an algorithm outside CEM §13's allowed set.
    """
    if algorithm not in _ALGORITHMS:
        raise UnsupportedDigestAlgorithm(f"unsupported integrity algorithm '{algorithm}'")
    digest = hashlib.new(_ALGORITHMS[algorithm])
    size = 0
    async for chunk in stream:
        digest.update(chunk)
        size += len(chunk)
    return StreamDigest(algorithm=algorithm, hex_digest=digest.hexdigest(), size_bytes=size)


def digests_match(expected: str, computed: str) -> bool:
    """Constant-time, case-insensitive comparison of two hex digests.

    Returns ``False`` (never raises) for malformed input, so a caller cannot be tricked into
    treating an unparseable expectation as a match.
    """
    if not expected or not computed or len(expected) != len(computed):
        return False
    if not all(char in _HEX_DIGITS for char in expected + computed):
        return False
    return hmac.compare_digest(expected.lower(), computed.lower())


__all__ = [
    "StreamDigest",
    "UnsupportedDigestAlgorithm",
    "compute_stream_digest",
    "digests_match",
    "is_valid_digest",
]
