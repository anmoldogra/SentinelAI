"""Timestamped anchors, end to end through the anchor service — ADR-0003 §3, Wave 1.3c.

Where `test_tsa.py` proves the token verifier, this proves the *integration*: that a cut anchor
carries a token over its own Merkle root, that the token survives into the WORM document, that the
Verification Engine reads it back and verifies it, and — the property that matters most in practice
—
that all of this degrades safely when no TSA exists.

The air-gapped path gets the most attention here, because it is the one a deployment cannot opt out
of: `deployment-architecture.md`'s zero-egress rule means the air-gapped and classified
profiles will *never* have a TSA, so an implementation that quietly made anchoring depend on one
would break the profiles this platform most needs to support.
"""

from __future__ import annotations

import base64
import json
import uuid
from datetime import UTC, datetime

import pytest

from sentinelai.platform.crypto.anchoring import (
    AnchorBatch,
    LedgerAnchorService,
    anchor_document,
)
from sentinelai.platform.crypto.ledger import LEDGER_AUDIT, LedgerSigner
from sentinelai.platform.crypto.tsa import TsaError, VerifiedTimestamp
from sentinelai.platform.crypto.verification import (
    AnchorView,
    Finding,
    LedgerVerifier,
    VerificationState,
)
from tests.fixtures.fake_object_storage import FakeObjectStorage
from tests.fixtures.fake_tsa import FakeTsa
from tests.fixtures.kms import kms_for_tests

_BUCKET = "sentinelai-anchors"
_HASHES = tuple(f"{i:064x}" for i in range(1, 5))


class _FakeAuthority:
    """A ``TimestampAuthority`` backed by the in-process TSA."""

    def __init__(self, tsa: FakeTsa) -> None:
        self._tsa = tsa
        self.messages: list[bytes] = []

    async def timestamp(self, message: bytes) -> tuple[bytes, VerifiedTimestamp]:
        self.messages.append(message)
        token = self._tsa.token_for(message)
        return token, VerifiedTimestamp(
            gen_time=datetime.now(UTC),
            serial_number=1,
            hash_algo="sha256",
            signer_subject="CN=SentinelAI Test TSA",
            policy=None,
        )


class _BrokenAuthority:
    """An authority that is configured but unreachable — the common production failure."""

    async def timestamp(self, message: bytes) -> tuple[bytes, VerifiedTimestamp]:
        raise TsaError("the TSA at https://tsa.example/tsr could not be reached: timed out")


def _service(storage: FakeObjectStorage, authority: object | None = None) -> LedgerAnchorService:
    return LedgerAnchorService(
        kms_for_tests(),
        storage,
        bucket=_BUCKET,
        timestamp_authority=authority,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------------------
# With a TSA
# ---------------------------------------------------------------------------------------


async def test_a_published_anchor_is_timestamped_over_its_own_merkle_root() -> None:
    """The token must cover the root, not the batch or the document.

    Timestamping anything else would leave an auditor unable to say what was attested to without
    extra context — and the root is exactly what the anchor signature already covers.
    """
    storage = FakeObjectStorage()
    tsa = FakeTsa()
    authority = _FakeAuthority(tsa)

    anchor = await _service(storage, authority).publish(
        AnchorBatch(ledger=LEDGER_AUDIT, entry_hashes=_HASHES)
    )

    assert anchor.is_timestamped
    assert anchor.tsa_token is not None
    assert authority.messages == [anchor.merkle_root.encode("ascii")]
    assert anchor.tsa_gen_time is not None
    assert anchor.tsa_signer == "CN=SentinelAI Test TSA"


async def test_the_token_travels_inside_the_worm_document() -> None:
    """The document must stand alone.

    An auditor holding only the WORM object and the public keys has to be able to answer "when was
    this committed?" without the database — the database being the thing under suspicion.
    """
    storage = FakeObjectStorage()
    tsa = FakeTsa()

    anchor = await _service(storage, _FakeAuthority(tsa)).publish(
        AnchorBatch(ledger=LEDGER_AUDIT, entry_hashes=_HASHES)
    )
    document = json.loads(anchor_document(anchor))

    assert "tsa_token" in document
    assert base64.b64decode(document["tsa_token"]) == anchor.tsa_token
    assert document["tsa_gen_time"].endswith("Z")
    assert document["merkle_root"] == anchor.merkle_root


async def test_a_timestamped_anchor_verifies_through_the_verification_engine() -> None:
    """The full loop: cut with a token, read it back, verify it against the trust anchors."""
    storage = FakeObjectStorage()
    tsa = FakeTsa()
    anchor = await _service(storage, _FakeAuthority(tsa)).publish(
        AnchorBatch(ledger=LEDGER_AUDIT, entry_hashes=_HASHES)
    )

    view = AnchorView(
        anchor_id=anchor.anchor_id,
        ledger=anchor.ledger,
        merkle_root=anchor.merkle_root,
        first_entry_hash=anchor.first_entry_hash,
        last_entry_hash=anchor.last_entry_hash,
        entry_count=anchor.entry_count,
        signature_envelope=anchor.signature.envelope,
        worm_object_ref=anchor.worm_object_ref,
        tsa_token=anchor.tsa_token,
    )
    report = await LedgerVerifier(
        LedgerSigner(kms_for_tests()), tsa_trust_anchors=tsa.trust_anchors
    ).verify_chain(ledger=LEDGER_AUDIT, entries=[], anchors=[view], chain_entry_hashes=_HASHES)

    assert report.anchors[0].state is VerificationState.VERIFIED
    assert report.anchors[0].timestamped is True
    assert report.anchors[0].tsa_gen_time is not None
    assert report.untimestamped_anchors == 0


async def test_a_forged_token_fails_verification_rather_than_being_ignored() -> None:
    """A token that does not verify is a FAILURE, never downgraded to "untimestamped".

    Treating a forgery as an absence would mean an attacker could attach junk and lose nothing.
    """
    storage = FakeObjectStorage()
    real = FakeTsa()
    stranger = FakeTsa()
    anchor = await _service(storage, _FakeAuthority(stranger)).publish(
        AnchorBatch(ledger=LEDGER_AUDIT, entry_hashes=_HASHES)
    )

    view = AnchorView(
        anchor_id=anchor.anchor_id,
        ledger=anchor.ledger,
        merkle_root=anchor.merkle_root,
        first_entry_hash=anchor.first_entry_hash,
        last_entry_hash=anchor.last_entry_hash,
        entry_count=anchor.entry_count,
        signature_envelope=anchor.signature.envelope,
        worm_object_ref=anchor.worm_object_ref,
        tsa_token=anchor.tsa_token,
    )
    # Verified against a DIFFERENT authority's roots — the signature is real but untrusted.
    report = await LedgerVerifier(
        LedgerSigner(kms_for_tests()), tsa_trust_anchors=real.trust_anchors
    ).verify_chain(ledger=LEDGER_AUDIT, entries=[], anchors=[view], chain_entry_hashes=_HASHES)

    assert report.anchors[0].state is VerificationState.FAILED
    assert Finding.TSA_TOKEN_INVALID in report.anchors[0].findings
    assert report.anchors[0].timestamped is False
    assert report.state is VerificationState.FAILED


async def test_a_token_present_with_no_configured_trust_store_fails_loudly() -> None:
    """ "We cannot check this token" is not "this token is fine".

    A verifier that skipped the check when unconfigured would accept any attacker's token.
    """
    storage = FakeObjectStorage()
    anchor = await _service(storage, _FakeAuthority(FakeTsa())).publish(
        AnchorBatch(ledger=LEDGER_AUDIT, entry_hashes=_HASHES)
    )

    view = AnchorView(
        anchor_id=anchor.anchor_id,
        ledger=anchor.ledger,
        merkle_root=anchor.merkle_root,
        first_entry_hash=anchor.first_entry_hash,
        last_entry_hash=anchor.last_entry_hash,
        entry_count=anchor.entry_count,
        signature_envelope=anchor.signature.envelope,
        worm_object_ref=anchor.worm_object_ref,
        tsa_token=anchor.tsa_token,
    )
    report = await LedgerVerifier(LedgerSigner(kms_for_tests())).verify_chain(
        ledger=LEDGER_AUDIT, entries=[], anchors=[view], chain_entry_hashes=_HASHES
    )

    assert Finding.TSA_TOKEN_INVALID in report.anchors[0].findings


# ---------------------------------------------------------------------------------------
# Without a TSA — the air-gapped path, which must never be a degraded-and-broken path
# ---------------------------------------------------------------------------------------


async def test_an_anchor_cuts_normally_with_no_authority_configured() -> None:
    """The air-gapped default. Anchoring must not depend on a third party being reachable.

    WORM already defeats truncation with no network beyond the object store; the TSA adds proof of
    *when*. Making the cut conditional on a TSA would trade the guarantee we control for one we
    do not.
    """
    storage = FakeObjectStorage()

    anchor = await _service(storage, None).publish(
        AnchorBatch(ledger=LEDGER_AUDIT, entry_hashes=_HASHES)
    )

    assert not anchor.is_timestamped
    assert anchor.tsa_token is None
    assert anchor.tsa_gen_time is None
    # Everything else is fully intact: the root, the signature, the WORM object.
    assert anchor.merkle_root
    assert await storage.exists(_BUCKET, anchor.worm_object_ref)


async def test_the_worm_document_omits_the_timestamp_fields_entirely_when_absent() -> None:
    """Omitted, not null: the document's shape states plainly whether a timestamp ever existed."""
    storage = FakeObjectStorage()
    anchor = await _service(storage, None).publish(
        AnchorBatch(ledger=LEDGER_AUDIT, entry_hashes=_HASHES)
    )

    document = json.loads(anchor_document(anchor))

    assert "tsa_token" not in document
    assert "tsa_gen_time" not in document
    assert document["merkle_root"] == anchor.merkle_root


async def test_an_unreachable_tsa_degrades_the_anchor_instead_of_failing_the_cut() -> None:
    """Fails OPEN — and this is the only failure in `publish` that does.

    An unsigned anchor or an unwritten WORM object would be a lie, so both abort. An untimestamped
    anchor is a weaker *true* statement. Aborting because someone else's TSA is down would let a
    third party's outage stop this platform committing to its own evidence.
    """
    storage = FakeObjectStorage()

    anchor = await _service(storage, _BrokenAuthority()).publish(
        AnchorBatch(ledger=LEDGER_AUDIT, entry_hashes=_HASHES)
    )

    assert not anchor.is_timestamped
    assert anchor.merkle_root
    assert await storage.exists(_BUCKET, anchor.worm_object_ref)


async def test_an_untimestamped_anchor_is_reported_but_does_not_degrade_the_verdict() -> None:
    """The decision that keeps `partial` meaningful.

    Every anchor cut before Wave 1.3c, and every anchor in every air-gapped deployment, carries no
    token. Degrading those to `partial` would flip a correct ledger to a hedged verdict permanently
    and drain the meaning out of the one state that is supposed to say "this cannot be proven".
    It is reported as a count instead.
    """
    storage = FakeObjectStorage()
    anchor = await _service(storage, None).publish(
        AnchorBatch(ledger=LEDGER_AUDIT, entry_hashes=_HASHES)
    )

    view = AnchorView(
        anchor_id=anchor.anchor_id,
        ledger=anchor.ledger,
        merkle_root=anchor.merkle_root,
        first_entry_hash=anchor.first_entry_hash,
        last_entry_hash=anchor.last_entry_hash,
        entry_count=anchor.entry_count,
        signature_envelope=anchor.signature.envelope,
        worm_object_ref=anchor.worm_object_ref,
        tsa_token=None,
    )
    report = await LedgerVerifier(LedgerSigner(kms_for_tests())).verify_chain(
        ledger=LEDGER_AUDIT, entries=[], anchors=[view], chain_entry_hashes=_HASHES
    )

    assert report.state is VerificationState.VERIFIED
    assert report.anchors[0].state is VerificationState.VERIFIED
    assert report.anchors[0].findings == ()
    assert report.anchors[0].timestamped is False
    assert report.untimestamped_anchors == 1


@pytest.mark.parametrize("ledger", [LEDGER_AUDIT, "ingestion.evidence_custody_events"])
async def test_both_ledgers_timestamp_through_the_same_path(ledger: str) -> None:
    """Anchoring is generic over what it commits to, and so is timestamping."""
    storage = FakeObjectStorage()
    tsa = FakeTsa()
    hashes = (uuid.uuid4().hex * 2, uuid.uuid4().hex * 2)

    anchor = await _service(storage, _FakeAuthority(tsa)).publish(
        AnchorBatch(ledger=ledger, entry_hashes=hashes)
    )

    assert anchor.ledger == ledger
    assert anchor.is_timestamped
