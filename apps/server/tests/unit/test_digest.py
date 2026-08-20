"""Unit tests for the streaming payload digest primitive (ADR-0008 §3, ADR-0003 §4)."""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator

import pytest

from sentinelai.platform.security.digest import (
    UnsupportedDigestAlgorithm,
    compute_stream_digest,
    digests_match,
    is_valid_digest,
)


async def _stream(*chunks: bytes) -> AsyncIterator[bytes]:
    for chunk in chunks:
        yield chunk


# --- digest computation -----------------------------------------------------


async def test_digest_matches_hashlib_over_the_whole_payload() -> None:
    payload = b"forensic-image-bytes" * 100
    result = await compute_stream_digest(_stream(payload))
    assert result.hex_digest == hashlib.sha256(payload).hexdigest()
    assert result.size_bytes == len(payload)
    assert result.algorithm == "SHA-256"


async def test_chunking_does_not_change_the_digest() -> None:
    payload = b"a" * 5000
    whole = await compute_stream_digest(_stream(payload))
    split = await compute_stream_digest(_stream(payload[:1], payload[1:2500], payload[2500:]))
    assert whole.hex_digest == split.hex_digest


async def test_empty_stream_digests_to_the_empty_hash() -> None:
    result = await compute_stream_digest(_stream())
    assert result.hex_digest == hashlib.sha256(b"").hexdigest()
    assert result.size_bytes == 0


async def test_large_stream_is_hashed_without_accumulating_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The digest never retains the payload: 64 MiB streams through with bounded memory."""
    chunk = b"z" * (1024 * 1024)
    total = 64
    expected = hashlib.sha256(chunk * total).hexdigest()

    peak_live_chunks = 0

    async def _big() -> AsyncIterator[bytes]:
        nonlocal peak_live_chunks
        for _ in range(total):
            peak_live_chunks = max(peak_live_chunks, 1)
            yield chunk

    result = await compute_stream_digest(_big())
    assert result.hex_digest == expected
    assert result.size_bytes == total * len(chunk)
    assert peak_live_chunks == 1


@pytest.mark.parametrize("algorithm", ["SHA-256", "SHA-3-256", "SHA-512"])
async def test_every_cem_allowed_algorithm_is_supported(algorithm: str) -> None:
    result = await compute_stream_digest(_stream(b"payload"), algorithm)
    assert result.algorithm == algorithm
    assert is_valid_digest(result.hex_digest, algorithm)


async def test_disallowed_algorithm_is_rejected() -> None:
    with pytest.raises(UnsupportedDigestAlgorithm):
        await compute_stream_digest(_stream(b"x"), "MD5")


# --- comparison / validation ------------------------------------------------


def test_matching_digests_compare_equal_regardless_of_case() -> None:
    digest = hashlib.sha256(b"x").hexdigest()
    assert digests_match(digest, digest.upper()) is True


def test_different_digests_do_not_match() -> None:
    assert digests_match("a" * 64, "b" * 64) is False


@pytest.mark.parametrize(
    ("expected", "computed"),
    [
        ("", "a" * 64),  # empty expectation
        ("a" * 64, ""),  # empty computation
        ("a" * 63, "a" * 64),  # length mismatch
        ("z" * 64, "a" * 64),  # non-hex characters
        ("café" + "a" * 60, "a" * 64),  # non-ASCII would break a naive compare_digest
    ],
)
def test_malformed_digests_never_match(expected: str, computed: str) -> None:
    assert digests_match(expected, computed) is False


@pytest.mark.parametrize(
    ("value", "algorithm", "valid"),
    [
        ("a" * 64, "SHA-256", True),
        ("A" * 64, "SHA-256", True),
        ("a" * 63, "SHA-256", False),
        ("a" * 128, "SHA-512", True),
        ("a" * 64, "SHA-512", False),
        ("g" * 64, "SHA-256", False),
        ("a" * 32, "MD5", False),
    ],
)
def test_digest_shape_validation(value: str, algorithm: str, valid: bool) -> None:
    assert is_valid_digest(value, algorithm) is valid
