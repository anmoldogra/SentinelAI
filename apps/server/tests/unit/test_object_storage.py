"""Object-storage unit tests (ADR-0008) — the in-memory fake against the shared contract + edges.

No network: these run everywhere. The real MinIO adapter is exercised by the same contract in
``tests/integration/test_object_storage_minio.py``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from sentinelai.platform.storage.port import ObjectStorage
from tests.contract.object_storage_contract import check_object_storage
from tests.fixtures.fake_object_storage import FakeObjectStorage


async def _empty() -> AsyncIterator[bytes]:
    for _ in ():
        yield b""  # pragma: no cover - an empty async generator


def test_fake_satisfies_the_object_storage_protocol() -> None:
    storage: ObjectStorage = FakeObjectStorage()  # structural (mypy) conformance check
    assert storage is not None


async def test_fake_satisfies_the_object_storage_contract() -> None:
    await check_object_storage(FakeObjectStorage(), bucket="unit-bucket")


async def test_head_and_get_on_missing_key_raise_keyerror() -> None:
    storage = FakeObjectStorage()
    await storage.ensure_bucket("b")
    with pytest.raises(KeyError):
        await storage.head("b", "absent")
    with pytest.raises(KeyError):
        _ = [chunk async for chunk in storage.get_stream("b", "absent")]


async def test_empty_object_round_trips() -> None:
    storage = FakeObjectStorage()
    await storage.ensure_bucket("b")
    await storage.put_stream("b", "empty", _empty())
    assert (await storage.head("b", "empty")).size == 0
    assert [chunk async for chunk in storage.get_stream("b", "empty")] in ([], [b""])


async def test_content_type_is_recorded() -> None:
    storage = FakeObjectStorage()
    await storage.ensure_bucket("b")
    await storage.put_stream("b", "k", _content(b"x"), content_type="application/pdf")
    assert (await storage.head("b", "k")).content_type == "application/pdf"


async def _content(*chunks: bytes) -> AsyncIterator[bytes]:
    for chunk in chunks:
        yield chunk
