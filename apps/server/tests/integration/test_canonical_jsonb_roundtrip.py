"""Canonical encoding survives a real PostgreSQL JSONB round-trip — ADR-0003 §2, Wave 1.1.

The determinism claim that matters for evidence is not "the same dict encodes the same way twice
in one process" — the unit suite proves that against a fake-free, pure-Python corpus. It is that
a value **written to the database and read back** still produces the identical preimage, because
that is what the Verification Engine (Wave 1.4) will actually do years after the write: re-read a
stored row and recompute its hash.

``jsonb`` is the specific hazard. Postgres does not store JSON text; it parses to a binary form
and **discards key order, whitespace, and duplicate keys**, then hands back keys in its own order
(by key length, then bytewise) — which is neither insertion order nor JCS order. A preimage built
from ``json.dumps`` of a round-tripped value therefore changes, silently, with no application
change. That is the defect this test exists to catch, so it asserts both directions: that the
database really does reorder, *and* that canonicalization absorbs it.

Requires a real Postgres — ``jsonb`` semantics are the thing under test, so a fake would prove
nothing. Skips cleanly when none is reachable, in a throwaway database that is dropped
afterwards. Never fakes a pass.
"""

from __future__ import annotations

import json
import os
import random
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from sentinelai.platform.config import settings
from sentinelai.platform.crypto.canonical import canonicalize

_URL = os.getenv("TEST_DATABASE_URL", settings.database_url)


async def _reachable(url: str) -> bool:
    try:
        # Explicit short timeout: this probe decides skip-vs-run and must never stall the suite.
        engine = create_async_engine(url, connect_args={"timeout": 3})
        try:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
        finally:
            await engine.dispose()
        return True
    except Exception:
        return False


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    if not await _reachable(_URL):
        pytest.skip(f"no Postgres reachable at {_URL.split('@')[-1]} — set TEST_DATABASE_URL")

    name = f"sentinelai_jcstest_{uuid.uuid4().hex[:8]}"
    admin = create_async_engine(_URL, isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as conn:
            await conn.execute(text(f'CREATE DATABASE "{name}"'))
    finally:
        await admin.dispose()

    target = create_async_engine(_URL.rsplit("/", 1)[0] + f"/{name}")
    try:
        async with target.begin() as conn:
            await conn.execute(text("CREATE TABLE probe (id int primary key, doc jsonb)"))
        yield target
    finally:
        await target.dispose()
        admin = create_async_engine(_URL, isolation_level="AUTOCOMMIT")
        try:
            async with admin.connect() as conn:
                await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        finally:
            await admin.dispose()


async def _roundtrip(engine: AsyncEngine, value: Any, row_id: int) -> Any:
    """Write ``value`` as jsonb and read back whatever Postgres decides to give us."""
    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO probe (id, doc) VALUES (:i, CAST(:d AS jsonb))"),
            {"i": row_id, "d": json.dumps(value)},
        )
    async with engine.connect() as conn:
        raw = await conn.execute(text("SELECT doc::text FROM probe WHERE id = :i"), {"i": row_id})
        return json.loads(raw.scalar_one())


# The keys are chosen so Postgres's jsonb ordering (length, then bytewise) disagrees with both
# insertion order and JCS's UTF-16 code-unit order — otherwise the test could pass vacuously.
_REORDERED = {
    "zulu": 1,
    "a": 2,
    "mike": 3,
    "bb": 4,
    "alpha_long_key": 5,
    "Z": 6,
}


async def test_postgres_really_does_reorder_jsonb_keys(engine: AsyncEngine) -> None:
    """Guards the guard. If jsonb ever preserved insertion order, the test below would be moot."""
    returned = await _roundtrip(engine, _REORDERED, 1)
    assert list(returned) != list(_REORDERED), "jsonb no longer reorders — revisit this suite"
    # And it is not JCS order either, which is the whole reason canonicalization is needed.
    assert list(returned) != sorted(_REORDERED, key=lambda k: k.encode("utf-16-be"))


async def test_canonical_bytes_survive_a_jsonb_roundtrip(engine: AsyncEngine) -> None:
    before = canonicalize(_REORDERED)
    after = canonicalize(await _roundtrip(engine, _REORDERED, 2))
    assert before == after


async def test_naive_encoding_would_have_diverged(engine: AsyncEngine) -> None:
    """The concrete regression: ``json.dumps`` without sorting changes across the round-trip.

    This is not a strawman — it is what an entry hash built from an un-canonicalized dict would
    do, and it is why ADR-0003 §2 makes the encoding a first-class, versioned concern.
    """
    returned = await _roundtrip(engine, _REORDERED, 3)
    assert json.dumps(_REORDERED).encode() != json.dumps(returned).encode()


_CORPUS_KEYS = "abcXYZ_012-é€"


def _random_doc(rng: random.Random, depth: int = 0) -> Any:
    if depth >= 3:
        return rng.choice([None, True, False, rng.randint(-1000, 1000), 1.5, "leaf", ""])
    kind = rng.choice(["dict", "list", "scalar"])
    if kind == "scalar":
        return rng.choice([None, True, rng.randint(-(2**40), 2**40), 0.1, 1e-7, "x"])
    if kind == "list":
        return [_random_doc(rng, depth + 1) for _ in range(rng.randint(0, 4))]
    return {
        "".join(rng.choice(_CORPUS_KEYS) for _ in range(rng.randint(1, 9))): _random_doc(
            rng, depth + 1
        )
        for _ in range(rng.randint(0, 6))
    }


async def test_property_corpus_survives_jsonb_roundtrip(engine: AsyncEngine) -> None:
    """200 generated documents, each written and read back, must canonicalize unchanged."""
    documents = [_random_doc(random.Random(seed)) for seed in range(200)]
    assert len({canonicalize(d) for d in documents}) > 100, "corpus is not varied enough"

    for index, document in enumerate(documents):
        returned = await _roundtrip(engine, document, 1000 + index)
        assert canonicalize(returned) == canonicalize(document), f"diverged on document {index}"


async def test_numbers_survive_jsonb_normalisation(engine: AsyncEngine) -> None:
    """jsonb stores numbers as ``numeric``, so 1 and 1.0 can come back either way.

    JCS renders both as ``1``, which is what makes the preimage stable across that normalisation.
    """
    returned = await _roundtrip(engine, {"a": 1, "b": 1.0, "c": 1e21, "d": 0.1}, 4)
    assert canonicalize(returned) == canonicalize({"a": 1, "b": 1.0, "c": 1e21, "d": 0.1})
    assert canonicalize(returned).startswith(b'{"a":1,"b":1,')


async def test_unicode_survives_jsonb_roundtrip(engine: AsyncEngine) -> None:
    """Non-ASCII must come back as the same code points and hash identically."""
    document = {"é": "€", "note": "\U0001f600", "דּ": "dalet"}
    returned = await _roundtrip(engine, document, 5)
    assert canonicalize(returned) == canonicalize(document)
