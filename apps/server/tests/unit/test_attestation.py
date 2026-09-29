"""ADR-0003 §3 independent attestation — the pure half, with no bucket and no KMS.

The point of keeping :func:`reconcile_anchors` pure is that the interesting cases are all
*archives that disagree with a database*, and constructing one of those against real infrastructure
is slow and awkward. Here they are three lines each.

``test_attestation_db.py`` covers the same findings end to end against real Postgres and a real
Object Lock bucket, including the DR scenario. Neither file substitutes for the other: this one
proves the comparison logic, that one proves the wiring.
"""

from __future__ import annotations

import base64
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from sentinelai.platform.crypto.anchoring import (
    ANCHOR_DOCUMENT_VERSION,
    anchor_document,
    anchor_object_key,
)
from sentinelai.platform.crypto.attestation import (
    AnchorDocument,
    AnchorDocumentError,
    AnchorDocumentUnknownVersion,
    AttestationFinding,
    MalformedAnchorObject,
    WormAnchorReader,
    anchor_prefix,
    build_attestation_report,
    parse_anchor_document,
    reconcile_anchors,
)
from sentinelai.platform.crypto.ledger import LEDGER_AUDIT, LedgerSignature
from sentinelai.platform.crypto.merkle import MERKLE_HASH_ALGO, build_tree
from sentinelai.platform.crypto.verification import AnchorView, VerificationState
from tests.fixtures.fake_object_storage import FakeObjectStorage

_ROOT = "a" * 64
_FIRST = "1" * 64
_LAST = "9" * 64
_ENVELOPE = b"envelope-bytes"


def _document(
    *,
    anchor_id: UUID | None = None,
    root: str = _ROOT,
    first: str = _FIRST,
    last: str = _LAST,
    count: int = 3,
    envelope: bytes = _ENVELOPE,
    key: str = "anchors/x/2026/09/29/a.json",
) -> AnchorDocument:
    return AnchorDocument(
        anchor_id=anchor_id or uuid4(),
        ledger=LEDGER_AUDIT,
        merkle_root=root,
        merkle_hash_algo=MERKLE_HASH_ALGO,
        first_entry_hash=first,
        last_entry_hash=last,
        entry_count=count,
        created_at="2026-09-29T00:00:00Z",
        signature_envelope=envelope,
        sig_alg="Ed25519",
        key_id="evidence_root/default/1",
        object_key=key,
    )


def _row(document: AnchorDocument, **overrides: object) -> AnchorView:
    """The `ledger_anchors` row that agrees with ``document``, unless an override disagrees."""
    fields: dict[str, object] = {
        "anchor_id": document.anchor_id,
        "ledger": document.ledger,
        "merkle_root": document.merkle_root,
        "first_entry_hash": document.first_entry_hash,
        "last_entry_hash": document.last_entry_hash,
        "entry_count": document.entry_count,
        "signature_envelope": document.signature_envelope,
        "worm_object_ref": document.object_key,
    }
    fields.update(overrides)
    return AnchorView(**fields)  # type: ignore[arg-type]


# --- the prefix must stay in step with the key scheme ----------------------------------------


def test_anchor_prefix_matches_the_key_the_writer_produces() -> None:
    """A prefix that drifted from `anchor_object_key` would list nothing — and an empty listing
    reads as "this ledger has no anchors", which passes attestation while proving nothing."""
    key = anchor_object_key(LEDGER_AUDIT, uuid4(), datetime(2026, 9, 29, tzinfo=UTC))
    assert key.startswith(anchor_prefix(LEDGER_AUDIT))


def test_anchor_prefix_does_not_match_another_ledgers_keys() -> None:
    """Non-vacuity for the test above: the prefix must actually discriminate between ledgers."""
    key = anchor_object_key("other.ledger", uuid4(), datetime(2026, 9, 29, tzinfo=UTC))
    assert not key.startswith(anchor_prefix(LEDGER_AUDIT))


# --- parsing is the inverse of what the writer publishes -------------------------------------


def test_parse_round_trips_the_document_the_anchor_writer_emits() -> None:
    """The one test that keeps the reader and the writer honest about each other.

    Built from `anchor_document` rather than from a hand-written fixture on purpose: a fixture would
    keep passing after the writer's field set changed, and the reader would then be parsing a shape
    nothing produces.
    """
    from sentinelai.platform.crypto.anchoring import PublishedAnchor

    tree = build_tree([_FIRST, _LAST])
    published = PublishedAnchor(
        anchor_id=uuid4(),
        ledger=LEDGER_AUDIT,
        merkle_root=tree.root,
        merkle_hash_algo=MERKLE_HASH_ALGO,
        first_entry_hash=_FIRST,
        last_entry_hash=_LAST,
        entry_count=2,
        created_at=datetime(2026, 9, 29, 12, tzinfo=UTC),
        signature=LedgerSignature(envelope=_ENVELOPE, sig_alg="Ed25519", key_id="k/1"),
        worm_object_ref="anchors/x/2026/09/29/a.json",
    )
    parsed = parse_anchor_document(anchor_document(published), object_key=published.worm_object_ref)

    assert parsed.anchor_id == published.anchor_id
    assert parsed.ledger == published.ledger
    assert parsed.merkle_root == published.merkle_root
    assert parsed.first_entry_hash == _FIRST
    assert parsed.last_entry_hash == _LAST
    assert parsed.entry_count == 2
    assert parsed.signature_envelope == _ENVELOPE
    assert parsed.sig_alg == "Ed25519"
    assert parsed.tsa_token is None


def test_parse_recovers_the_tsa_token_when_the_writer_included_one() -> None:
    from sentinelai.platform.crypto.anchoring import PublishedAnchor

    published = PublishedAnchor(
        anchor_id=uuid4(),
        ledger=LEDGER_AUDIT,
        merkle_root=_ROOT,
        merkle_hash_algo=MERKLE_HASH_ALGO,
        first_entry_hash=_FIRST,
        last_entry_hash=_LAST,
        entry_count=2,
        created_at=datetime(2026, 9, 29, tzinfo=UTC),
        signature=LedgerSignature(envelope=_ENVELOPE, sig_alg="Ed25519", key_id="k/1"),
        worm_object_ref="k.json",
        tsa_token=b"\x30\x82der-token",
        tsa_gen_time=datetime(2026, 9, 29, tzinfo=UTC),
        tsa_serial_number=7,
    )
    parsed = parse_anchor_document(anchor_document(published), object_key="k.json")
    assert parsed.tsa_token == b"\x30\x82der-token"


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param(b"not json at all", id="not-json"),
        pytest.param(b'["a", "list"]', id="json-but-not-an-object"),
        pytest.param(b"{}", id="empty-object"),
    ],
)
def test_parse_refuses_anything_that_is_not_an_anchor(raw: bytes) -> None:
    with pytest.raises(AnchorDocumentError):
        parse_anchor_document(raw, object_key="k.json")


def _valid_payload(**overrides: object) -> bytes:
    payload: dict[str, object] = {
        "v": ANCHOR_DOCUMENT_VERSION,
        "anchor_id": str(uuid4()),
        "ledger": LEDGER_AUDIT,
        "merkle_root": _ROOT,
        "merkle_hash_algo": MERKLE_HASH_ALGO,
        "first_entry_hash": _FIRST,
        "last_entry_hash": _LAST,
        "entry_count": 3,
        "created_at": "2026-09-29T00:00:00Z",
        "signature": base64.b64encode(_ENVELOPE).decode(),
        "sig_alg": "Ed25519",
        "key_id": "k/1",
    }
    payload.update(overrides)
    return json.dumps(payload).encode()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("anchor_id", "not-a-uuid"),
        ("entry_count", 0),
        ("entry_count", -1),
        ("entry_count", "3"),
        ("entry_count", True),
        ("signature", "not!base64!"),
        ("merkle_root", ""),
        ("ledger", 42),
        ("tsa_token", "not!base64!"),
        ("tsa_token", 7),
    ],
)
def test_parse_is_strict_field_by_field(field: str, value: object) -> None:
    """A lenient parser would let a partly-corrupt archive read as a valid commitment."""
    with pytest.raises(AnchorDocumentError):
        parse_anchor_document(_valid_payload(**{field: value}), object_key="k.json")


def test_entry_count_true_is_rejected_because_bool_is_an_int() -> None:
    """`isinstance(True, int)` is True in Python, so the bool guard is load-bearing here."""
    with pytest.raises(AnchorDocumentError, match="entry_count"):
        parse_anchor_document(_valid_payload(entry_count=True), object_key="k.json")


def test_an_unknown_document_version_is_its_own_error_type() -> None:
    """Distinguished because the caller turns it into a `partial`, not a `failed`: guessing a future
    field layout would produce a mismatch and report a valid anchor as forged."""
    with pytest.raises(AnchorDocumentUnknownVersion):
        parse_anchor_document(_valid_payload(v=ANCHOR_DOCUMENT_VERSION + 1), object_key="k.json")


# --- reconciliation -------------------------------------------------------------------------


def test_an_agreeing_pair_is_verified() -> None:
    document = _document()
    results = reconcile_anchors(
        documents=[document],
        database_anchors=[_row(document)],
        signature_valid={document.anchor_id: True},
    )
    assert [r.state for r in results] == [VerificationState.VERIFIED]
    assert results[0].findings == ()
    assert results[0].in_worm and results[0].in_database


def test_an_anchor_in_worm_with_no_database_row_is_partial_not_failed() -> None:
    """The distinction this module exists to draw.

    `LedgerAnchorService.publish` writes the object *before* the row, so a crash between the two
    leaves an orphan object — an interrupted job, not tampering. Reporting it as `failed` would
    alarm on every killed anchor cut; reporting it as clean would hide the restore case. It is
    `partial`, and the `failed` verdict comes from the chain check when the anchor's range has also
    gone (see the DR scenario in test_attestation_db.py).
    """
    document = _document()
    results = reconcile_anchors(
        documents=[document], database_anchors=[], signature_valid={document.anchor_id: True}
    )
    assert results[0].state is VerificationState.PARTIAL
    assert results[0].findings == (AttestationFinding.ANCHOR_MISSING_FROM_DATABASE,)
    assert results[0].in_worm and not results[0].in_database


def test_a_database_row_with_no_worm_object_is_failed() -> None:
    """Under COMPLIANCE-mode Object Lock the object should have been undeletable, so its absence
    means either the lock was never real or something with bucket administration removed it."""
    document = _document()
    results = reconcile_anchors(documents=[], database_anchors=[_row(document)], signature_valid={})
    assert results[0].state is VerificationState.FAILED
    assert results[0].findings == (AttestationFinding.ANCHOR_OBJECT_MISSING,)
    assert results[0].in_database and not results[0].in_worm


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("merkle_root", "b" * 64),
        ("first_entry_hash", "2" * 64),
        ("last_entry_hash", "8" * 64),
        ("entry_count", 99),
        ("ledger", "ingestion.evidence_custody_events"),
        ("signature_envelope", b"different-envelope"),
    ],
)
def test_any_disagreement_between_the_stores_is_failed_and_names_the_field(
    field: str, value: object
) -> None:
    """The bucket's copy is the immutable one, so a disagreement reports an edited database row —
    and the report names *which* field, because a changed `entry_count` and a substituted
    `merkle_root` lead an operator to different conclusions."""
    document = _document()
    results = reconcile_anchors(
        documents=[document],
        database_anchors=[_row(document, **{field: value})],
        signature_valid={document.anchor_id: True},
    )
    assert results[0].state is VerificationState.FAILED
    assert AttestationFinding.ANCHOR_DOCUMENT_MISMATCH in results[0].findings
    assert results[0].detail is not None
    assert field in results[0].detail


def test_signature_envelope_is_compared_even_when_every_other_field_agrees() -> None:
    """A row carrying a different envelope from the published one is a replay or a fabrication, and
    it is invisible unless the envelope itself is compared."""
    document = _document()
    results = reconcile_anchors(
        documents=[document],
        database_anchors=[_row(document, signature_envelope=b"forged")],
        signature_valid={document.anchor_id: True},
    )
    assert results[0].detail is not None
    assert "signature_envelope" in results[0].detail


def test_an_invalid_document_signature_is_failed() -> None:
    document = _document()
    results = reconcile_anchors(
        documents=[document],
        database_anchors=[_row(document)],
        signature_valid={document.anchor_id: False},
    )
    assert results[0].state is VerificationState.FAILED
    assert AttestationFinding.ANCHOR_DOCUMENT_SIGNATURE_INVALID in results[0].findings


def test_a_document_absent_from_the_signature_map_fails_closed() -> None:
    """A missing verdict must never be the same thing as a positive one."""
    document = _document()
    results = reconcile_anchors(
        documents=[document], database_anchors=[_row(document)], signature_valid={}
    )
    assert AttestationFinding.ANCHOR_DOCUMENT_SIGNATURE_INVALID in results[0].findings


def test_signature_valid_defaults_to_failing_closed_when_omitted_entirely() -> None:
    document = _document()
    results = reconcile_anchors(documents=[document], database_anchors=[_row(document)])
    assert results[0].state is VerificationState.FAILED


def test_a_malformed_object_is_failed_and_an_unknown_version_is_partial() -> None:
    results = reconcile_anchors(
        documents=[],
        malformed=[
            MalformedAnchorObject(object_key="bad.json", reason="not JSON"),
            MalformedAnchorObject(object_key="future.json", reason="v2", unknown_version=True),
        ],
        database_anchors=[],
    )
    by_key = {r.object_key: r for r in results}
    assert by_key["bad.json"].state is VerificationState.FAILED
    assert by_key["bad.json"].findings == (AttestationFinding.ANCHOR_DOCUMENT_MALFORMED,)
    assert by_key["future.json"].state is VerificationState.PARTIAL
    assert by_key["future.json"].anchor_id is None


# --- the rolled-up report -------------------------------------------------------------------


def test_report_counts_both_directions_of_disagreement() -> None:
    orphan = _document(key="orphan.json")
    agreeing = _document(key="agreeing.json")
    missing = _document(key="missing-object.json")
    attestations = reconcile_anchors(
        documents=[orphan, agreeing],
        malformed=[MalformedAnchorObject(object_key="junk.json", reason="not JSON")],
        database_anchors=[_row(agreeing), _row(missing)],
        signature_valid={orphan.anchor_id: True, agreeing.anchor_id: True},
    )
    report = build_attestation_report(
        ledger=LEDGER_AUDIT, attestations=attestations, database_anchors=2
    )

    assert report.worm_anchors == 2
    assert report.database_anchors == 2
    assert report.reconciled == 1
    assert report.missing_from_database == 1
    assert report.missing_from_worm == 1
    assert report.malformed_objects == 1
    assert report.state is VerificationState.FAILED
    assert report.is_failed


def test_report_is_verified_only_when_every_anchor_agrees() -> None:
    document = _document()
    report = build_attestation_report(
        ledger=LEDGER_AUDIT,
        attestations=reconcile_anchors(
            documents=[document],
            database_anchors=[_row(document)],
            signature_valid={document.anchor_id: True},
        ),
        database_anchors=1,
    )
    assert report.state is VerificationState.VERIFIED
    assert not report.is_failed
    assert report.findings == ()


def test_report_is_partial_when_the_only_problem_is_an_orphan_object() -> None:
    document = _document()
    report = build_attestation_report(
        ledger=LEDGER_AUDIT,
        attestations=reconcile_anchors(
            documents=[document],
            database_anchors=[],
            signature_valid={document.anchor_id: True},
        ),
        database_anchors=0,
    )
    assert report.state is VerificationState.PARTIAL
    assert report.findings == (AttestationFinding.ANCHOR_MISSING_FROM_DATABASE,)


def test_an_empty_archive_and_an_empty_database_agree_vacuously() -> None:
    """A deployment whose cutter has never run has nothing to reconcile. Reporting `failed` here
    would make every fresh install look tampered with; the unanchored-entry gauge is what says the
    cutter has stopped."""
    report = build_attestation_report(ledger=LEDGER_AUDIT, attestations=[], database_anchors=0)
    assert report.state is VerificationState.VERIFIED
    assert report.worm_anchors == 0


def test_to_anchor_view_carries_the_object_key_as_the_reference() -> None:
    """The verifier's `AnchorView.worm_object_ref` must name the object actually read, not a value
    copied out of the database — otherwise a finding would point an auditor at the wrong file."""
    document = _document(key="anchors/l/2026/09/29/z.json")
    view = document.to_anchor_view()
    assert view.worm_object_ref == "anchors/l/2026/09/29/z.json"
    assert view.merkle_root == document.merkle_root
    assert view.entry_count == document.entry_count
    assert view.signature_envelope == document.signature_envelope


# --- the reader's two unhappy paths ----------------------------------------------------------


class _VanishingStorage(FakeObjectStorage):
    """Lists a key that is gone by the time it is fetched — a deletion racing the listing."""

    async def list_prefix(self, bucket: str, prefix: str) -> AsyncIterator[str]:
        async for key in super().list_prefix(bucket, prefix):
            yield key
        yield f"{prefix}2026/09/29/{uuid4()}.json"


async def test_an_anchor_that_vanishes_between_listing_and_fetch_is_reported() -> None:
    """Reported rather than skipped: a vanishing anchor is the event this module exists for."""
    storage = _VanishingStorage()
    await storage.ensure_worm_bucket("b")
    documents, malformed = await WormAnchorReader(storage, bucket="b").read(LEDGER_AUDIT)

    assert documents == []
    assert len(malformed) == 1
    assert "could not be read" in malformed[0].reason
    assert not malformed[0].unknown_version  # a failure, not a partial


async def test_the_reader_marks_a_future_document_version_as_a_partial() -> None:
    """The `unknown_version` flag keeps a later writer's anchor out of the `failed` bucket."""
    storage = FakeObjectStorage()
    await storage.ensure_worm_bucket("b")
    prefix = anchor_prefix(LEDGER_AUDIT)
    await storage.put_immutable(
        "b",
        f"{prefix}2026/09/29/{uuid4()}.json",
        _valid_payload(v=ANCHOR_DOCUMENT_VERSION + 1),
        retain_until=datetime(2036, 1, 1, tzinfo=UTC),
    )
    await storage.put_immutable(
        "b",
        f"{prefix}2026/09/29/{uuid4()}.json",
        b"not json",
        retain_until=datetime(2036, 1, 1, tzinfo=UTC),
    )
    documents, malformed = await WormAnchorReader(storage, bucket="b").read(LEDGER_AUDIT)

    assert documents == []
    flags = sorted(m.unknown_version for m in malformed)
    assert flags == [False, True]  # one malformed, one merely from the future


async def test_the_reader_parses_every_good_object_under_the_prefix() -> None:
    """Non-vacuity for the two tests above: the reader must actually read the happy path."""
    storage = FakeObjectStorage()
    await storage.ensure_worm_bucket("b")
    prefix = anchor_prefix(LEDGER_AUDIT)
    for _ in range(3):
        await storage.put_immutable(
            "b",
            f"{prefix}2026/09/29/{uuid4()}.json",
            _valid_payload(),
            retain_until=datetime(2036, 1, 1, tzinfo=UTC),
        )
    # An object outside the prefix must not be read as an anchor.
    await storage.put_immutable(
        "b", "other/thing.json", b"not json", retain_until=datetime(2036, 1, 1, tzinfo=UTC)
    )
    documents, malformed = await WormAnchorReader(storage, bucket="b").read(LEDGER_AUDIT)

    assert len(documents) == 3
    assert malformed == []
