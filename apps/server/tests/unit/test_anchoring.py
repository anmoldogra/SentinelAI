"""Anchor construction, publication, and verification — ADR-0003 §3.

No database: building a root, signing it, and writing it to WORM involves neither. The
database-level attacks (truncation, restore-from-backup) live in
``tests/integration/test_ledger_anchoring_db.py``, because those need a real ledger to remove rows
from. What is covered here is the anchor itself — what it commits to, what it publishes, and what
it refuses.

Real Ed25519 from the dev KMS provider throughout; a stub would make the signature assertions
vacuous.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

import pytest

from sentinelai.platform.crypto.anchoring import (
    ANCHOR_DOCUMENT_VERSION,
    DEFAULT_RETENTION_YEARS,
    AnchorBatch,
    AnchoringError,
    LedgerAnchorService,
    anchor_document,
    anchor_object_key,
    verify_batch_against_anchor,
)
from sentinelai.platform.crypto.merkle import merkle_root
from tests.fixtures.fake_object_storage import FakeObjectStorage
from tests.fixtures.kms import kms_for_tests

_LEDGER = "platform.audit_log"
_BUCKET = "sentinelai-anchors"
_NOW = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)


def _hashes(count: int) -> tuple[str, ...]:
    return tuple(hashlib.sha256(str(i).encode()).hexdigest() for i in range(count))


@pytest.fixture
def storage() -> FakeObjectStorage:
    return FakeObjectStorage()


@pytest.fixture
def service(storage: FakeObjectStorage) -> LedgerAnchorService:
    return LedgerAnchorService(kms_for_tests(), storage, bucket=_BUCKET)


# --------------------------------------------------------------------------------------
# Batches
# --------------------------------------------------------------------------------------


def test_an_empty_batch_is_refused_at_construction() -> None:
    """Anchoring nothing publishes a commitment that proves nothing while looking like proof."""
    with pytest.raises(AnchoringError, match="empty batch"):
        AnchorBatch(_LEDGER, ())


# --------------------------------------------------------------------------------------
# Publication
# --------------------------------------------------------------------------------------


async def test_publish_commits_to_the_batch_and_its_bounds(
    service: LedgerAnchorService,
) -> None:
    entries = _hashes(5)
    anchor = await service.publish(AnchorBatch(_LEDGER, entries), now=_NOW)

    assert anchor.merkle_root == merkle_root(list(entries))
    assert anchor.first_entry_hash == entries[0]
    assert anchor.last_entry_hash == entries[-1]
    assert anchor.entry_count == 5
    assert anchor.created_at == _NOW
    assert anchor.merkle_hash_algo == "SHA-256"


async def test_publish_writes_the_object_under_a_worm_retention_lock(
    service: LedgerAnchorService, storage: FakeObjectStorage
) -> None:
    """WORM is the whole mechanism. An anchor written as an ordinary object is deletable, and a
    deletable anchor detects nothing — the attacker deletes it along with the entries."""
    anchor = await service.publish(AnchorBatch(_LEDGER, _hashes(3)), now=_NOW)

    assert await storage.exists(_BUCKET, anchor.worm_object_ref)
    retain_until = storage.retentions[(_BUCKET, anchor.worm_object_ref)]
    assert retain_until.year == _NOW.year + DEFAULT_RETENTION_YEARS
    assert retain_until > _NOW


async def test_the_published_object_is_the_anchor_document(
    service: LedgerAnchorService, storage: FakeObjectStorage
) -> None:
    """What lands in WORM must be exactly what a later auditor verifies, byte for byte."""
    anchor = await service.publish(AnchorBatch(_LEDGER, _hashes(4)), now=_NOW)
    stored = b"".join(
        [chunk async for chunk in storage.get_stream(_BUCKET, anchor.worm_object_ref)]
    )
    assert stored == anchor_document(anchor)


async def test_the_anchor_document_stands_alone(service: LedgerAnchorService) -> None:
    """An auditor holding only this object and the public key must be able to verify it.

    That is the point of anchoring: the database is the thing being checked, so the anchor cannot
    depend on it.
    """
    anchor = await service.publish(AnchorBatch(_LEDGER, _hashes(4)), now=_NOW)
    document = json.loads(anchor_document(anchor))

    assert document["v"] == ANCHOR_DOCUMENT_VERSION
    assert document["created_at"] == "2026-09-08T12:00:00Z"
    assert set(document) == {
        "v",
        "anchor_id",
        "ledger",
        "merkle_root",
        "merkle_hash_algo",
        "first_entry_hash",
        "last_entry_hash",
        "entry_count",
        "created_at",
        "signature",
        "sig_alg",
        "key_id",
    }


async def test_the_document_is_canonical_json(service: LedgerAnchorService) -> None:
    """RFC 8785, like everything else that gets hashed or published here — so two implementations
    reading it agree on its bytes."""
    anchor = await service.publish(AnchorBatch(_LEDGER, _hashes(2)), now=_NOW)
    raw = anchor_document(anchor).decode()
    assert raw.startswith('{"anchor_id":')  # keys sorted, no whitespace
    assert " " not in raw.split('"ledger"')[0]


async def test_each_publication_is_distinct(service: LedgerAnchorService) -> None:
    entries = _hashes(3)
    a = await service.publish(AnchorBatch(_LEDGER, entries), now=_NOW)
    b = await service.publish(AnchorBatch(_LEDGER, entries), now=_NOW)
    assert a.anchor_id != b.anchor_id
    assert a.worm_object_ref != b.worm_object_ref
    assert a.merkle_root == b.merkle_root  # same batch, same commitment


def test_object_keys_are_time_partitioned(service: LedgerAnchorService) -> None:
    """A bucket listing should be chronologically browsable — that is how an auditor uses it."""
    import uuid

    key = anchor_object_key(_LEDGER, uuid.UUID(int=1), _NOW)
    assert key == f"anchors/{_LEDGER}/2026/09/08/00000000-0000-0000-0000-000000000001.json"


async def test_a_storage_failure_raises_rather_than_returning_an_anchor(
    storage: FakeObjectStorage,
) -> None:
    """No object, no anchor. Returning one would let a caller record a claim that is false."""

    class BrokenStorage(FakeObjectStorage):
        async def put_immutable(self, *args: object, **kwargs: object) -> None:
            raise OSError("object store unreachable")

    service = LedgerAnchorService(kms_for_tests(), BrokenStorage(), bucket=_BUCKET)
    with pytest.raises(AnchoringError, match="could not be published"):
        await service.publish(AnchorBatch(_LEDGER, _hashes(2)), now=_NOW)


# --------------------------------------------------------------------------------------
# Signature over the anchor
# --------------------------------------------------------------------------------------


async def test_the_anchor_signature_verifies(service: LedgerAnchorService) -> None:
    anchor = await service.publish(AnchorBatch(_LEDGER, _hashes(6)), now=_NOW)
    assert await service.verify_signature(anchor) is True
    assert anchor.signature.sig_alg == "ED25519"


async def test_a_tampered_anchor_field_invalidates_the_signature(
    service: LedgerAnchorService,
) -> None:
    """Every field the signature binds, one at a time.

    An attacker who can write the anchor table would otherwise repoint an anchor at a different
    root or a narrower range and have it still verify.
    """
    import dataclasses

    anchor = await service.publish(AnchorBatch(_LEDGER, _hashes(6)), now=_NOW)
    for field, forged in [
        ("merkle_root", "f" * 64),
        ("first_entry_hash", "e" * 64),
        ("entry_count", 5),
        ("ledger", "ingestion.evidence_custody_events"),
    ]:
        mutated = dataclasses.replace(anchor, **{field: forged})
        assert await service.verify_signature(mutated) is False, f"forging {field} was not caught"


async def test_an_anchor_signature_is_not_a_ledger_entry_signature(
    service: LedgerAnchorService,
) -> None:
    """Domain separation: `anchor:<chain>` is distinct from `<chain>`, so a signature over an
    anchor cannot be presented as a signature over an entry."""
    from sentinelai.platform.crypto.ledger import LedgerSigner

    anchor = await service.publish(AnchorBatch(_LEDGER, _hashes(3)), now=_NOW)
    signer = LedgerSigner(kms_for_tests())
    assert (
        await signer.verify(
            ledger=_LEDGER,
            sequence=anchor.entry_count,
            prev_hash=anchor.first_entry_hash,
            entry_hash=anchor.merkle_root,
            envelope=anchor.signature.envelope,
        )
        is False
    )


# --------------------------------------------------------------------------------------
# Verifying a ledger against an anchor
# --------------------------------------------------------------------------------------


def test_an_unchanged_batch_verifies() -> None:
    entries = _hashes(7)
    assert verify_batch_against_anchor(
        entries, merkle_root=merkle_root(list(entries)), entry_count=7
    )


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (lambda e: e[:-1], "tail truncated"),
        (lambda e: e[:3], "restored from an older snapshot"),
        (lambda e: e[1:], "head removed"),
        (lambda e: (*e, "a" * 64), "entry appended"),
        (lambda e: (e[1], e[0], *e[2:]), "entries reordered"),
        (lambda e: ("f" * 64, *e[1:]), "entry replaced"),
    ],
)
def test_any_change_to_the_covered_range_is_detected(mutate: object, reason: str) -> None:
    entries = _hashes(7)
    root = merkle_root(list(entries))
    assert not verify_batch_against_anchor(
        mutate(entries),  # type: ignore[operator]
        merkle_root=root,
        entry_count=7,
    ), reason


def test_an_emptied_ledger_is_detected_rather_than_erroring() -> None:
    """The maximal truncation. It must be a verification failure, not an exception — a wiped
    ledger is a finding, and a crash would look like a bug in the verifier."""
    assert not verify_batch_against_anchor(
        [], merkle_root=merkle_root(list(_hashes(3))), entry_count=3
    )


def test_a_count_mismatch_short_circuits() -> None:
    """Checked separately so the caller can say *why* — 'three entries are missing' and 'one entry
    was altered' are different findings on a court-facing report, even though both are a root
    mismatch."""
    entries = _hashes(5)
    assert not verify_batch_against_anchor(
        entries, merkle_root=merkle_root(list(entries)), entry_count=4
    )
