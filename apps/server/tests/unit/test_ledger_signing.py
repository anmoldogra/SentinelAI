"""Ledger signing primitives — ADR-0003 §1.

Real Ed25519 throughout (the dev KMS provider), because every claim here is about what a
signature does and a stub would make all of them vacuous. The database-level attacks live in
``tests/integration/test_ledger_signatures_db.py``; this file covers the envelope, the signed
message, and the failure modes.
"""

from __future__ import annotations

import base64
import json

import pytest

from sentinelai.platform.crypto.exceptions import KmsUnavailable
from sentinelai.platform.crypto.ledger import (
    LEDGER_AUDIT,
    LEDGER_CUSTODY,
    SIGNATURE_ENVELOPE_VERSION,
    LedgerSignatureError,
    LedgerSigner,
    signed_message,
)
from tests.fixtures.kms import kms_for_tests

_PREV = "0" * 64
_ENTRY = "a" * 64


def _signer() -> LedgerSigner:
    return LedgerSigner(kms_for_tests())


# --------------------------------------------------------------------------------------
# The signed message
# --------------------------------------------------------------------------------------


def test_signed_message_is_canonical_json_not_concatenation() -> None:
    """ADR-0003 §1 writes this as ``sequence || prev || entry_hash``; it is built as JCS.

    Concatenating variable-length fields is ambiguous once hash widths can differ under crypto
    agility, so the encoding is structured. The exact bytes are asserted because an independent
    verifier has to reproduce them.
    """
    assert signed_message(
        ledger=LEDGER_AUDIT, sequence=None, prev_hash=_PREV, entry_hash=_ENTRY
    ) == (
        b'{"entry_hash":"' + _ENTRY.encode() + b'","ledger":"platform.audit_log",'
        b'"prev":"' + _PREV.encode() + b'","seq":null}'
    )


def test_signed_message_is_deterministic() -> None:
    a = signed_message(ledger=LEDGER_CUSTODY, sequence=3, prev_hash=_PREV, entry_hash=_ENTRY)
    b = signed_message(ledger=LEDGER_CUSTODY, sequence=3, prev_hash=_PREV, entry_hash=_ENTRY)
    assert a == b


@pytest.mark.parametrize(
    "change",
    [
        {"ledger": LEDGER_CUSTODY},
        {"sequence": 2},
        {"prev_hash": "b" * 64},
        {"entry_hash": "c" * 64},
    ],
)
def test_every_component_changes_the_signed_message(change: dict[str, object]) -> None:
    base = {"ledger": LEDGER_AUDIT, "sequence": 1, "prev_hash": _PREV, "entry_hash": _ENTRY}
    assert signed_message(**base) != signed_message(**{**base, **change})  # type: ignore[arg-type]


def test_a_null_sequence_is_distinct_from_zero() -> None:
    """The audit ledger has no sequence; that must not collide with a custody entry at seq 0."""
    assert signed_message(
        ledger=LEDGER_AUDIT, sequence=None, prev_hash=_PREV, entry_hash=_ENTRY
    ) != signed_message(ledger=LEDGER_AUDIT, sequence=0, prev_hash=_PREV, entry_hash=_ENTRY)


# --------------------------------------------------------------------------------------
# Signing and verifying
# --------------------------------------------------------------------------------------


async def test_sign_then_verify_round_trips() -> None:
    signer = _signer()
    signature = await signer.sign(
        ledger=LEDGER_AUDIT, sequence=None, prev_hash=_PREV, entry_hash=_ENTRY
    )
    assert await signer.verify(
        ledger=LEDGER_AUDIT,
        sequence=None,
        prev_hash=_PREV,
        entry_hash=_ENTRY,
        envelope=signature.envelope,
    )


async def test_signature_records_algorithm_and_versioned_key_id() -> None:
    signature = await _signer().sign(
        ledger=LEDGER_AUDIT, sequence=None, prev_hash=_PREV, entry_hash=_ENTRY
    )
    assert signature.sig_alg == "ED25519"
    provider, version, backend_ref = signature.key_id.split(":", 2)
    assert provider == "dev"
    assert int(version) >= 1
    # The purpose is encoded in the backend key name, so the evidence root is visibly distinct
    # from the event or storage roots even in a raw database dump.
    assert backend_ref == "evidence_root__default"


@pytest.mark.parametrize(
    "change",
    [
        {"ledger": LEDGER_CUSTODY},
        {"sequence": 1},
        {"prev_hash": "b" * 64},
        {"entry_hash": "c" * 64},
    ],
)
async def test_verification_fails_when_any_covered_field_changes(change: dict[str, object]) -> None:
    signer = _signer()
    base: dict[str, object] = {
        "ledger": LEDGER_AUDIT,
        "sequence": None,
        "prev_hash": _PREV,
        "entry_hash": _ENTRY,
    }
    signature = await signer.sign(**base)  # type: ignore[arg-type]
    assert not await signer.verify(**{**base, **change}, envelope=signature.envelope)  # type: ignore[arg-type]


async def test_two_entries_do_not_share_a_signature() -> None:
    signer = _signer()
    first = await signer.sign(
        ledger=LEDGER_AUDIT, sequence=None, prev_hash=_PREV, entry_hash="a" * 64
    )
    second = await signer.sign(
        ledger=LEDGER_AUDIT, sequence=None, prev_hash=_PREV, entry_hash="b" * 64
    )
    assert first.envelope != second.envelope
    assert not await signer.verify(
        ledger=LEDGER_AUDIT,
        sequence=None,
        prev_hash=_PREV,
        entry_hash="a" * 64,
        envelope=second.envelope,
    )


# --------------------------------------------------------------------------------------
# Failure modes — all of them return False or raise; none of them return True
# --------------------------------------------------------------------------------------


async def test_a_missing_envelope_is_not_authentic() -> None:
    """An unsigned entry must never verify. Callers distinguish unsigned from forged themselves."""
    assert not await _signer().verify(
        ledger=LEDGER_AUDIT, sequence=None, prev_hash=_PREV, entry_hash=_ENTRY, envelope=None
    )


@pytest.mark.parametrize(
    ("envelope", "reason"),
    [
        (b"", "empty"),
        (b"not json", "not JSON"),
        (b"[]", "not an object"),
        (b'{"v": 99, "sigs": []}', "unsupported version"),
        (b'{"v": 1, "sigs": []}', "no signatures"),
        (b'{"v": 1, "sigs": "nope"}', "signatures not a list"),
        (b'{"v": 1, "sigs": [{"alg": "ED25519"}]}', "header fields missing"),
        (b"\xff\xfe\x00", "not UTF-8"),
    ],
)
async def test_a_malformed_envelope_is_invalid_not_an_error(envelope: bytes, reason: str) -> None:
    """It was presented as a signature and it does not verify — that is an invalid signature."""
    assert not await _signer().verify(
        ledger=LEDGER_AUDIT, sequence=None, prev_hash=_PREV, entry_hash=_ENTRY, envelope=envelope
    ), reason


async def test_tampering_with_the_envelope_header_invalidates_it() -> None:
    """The header is what is actually signed, so editing any of it breaks the signature.

    This is the downgrade defense from ADR-0009 C1 seen from the ledger's side: an attacker cannot
    relabel an entry as signed under a different key or algorithm, because those claims are inside
    the signed bytes.
    """
    signer = _signer()
    signature = await signer.sign(
        ledger=LEDGER_AUDIT, sequence=None, prev_hash=_PREV, entry_hash=_ENTRY
    )
    parsed = json.loads(signature.envelope)
    parsed["sigs"][0]["kid"]["v"] = 99
    assert not await signer.verify(
        ledger=LEDGER_AUDIT,
        sequence=None,
        prev_hash=_PREV,
        entry_hash=_ENTRY,
        envelope=json.dumps(parsed).encode(),
    )


async def test_replacing_the_signature_bytes_invalidates_it() -> None:
    signer = _signer()
    signature = await signer.sign(
        ledger=LEDGER_AUDIT, sequence=None, prev_hash=_PREV, entry_hash=_ENTRY
    )
    parsed = json.loads(signature.envelope)
    parsed["sigs"][0]["sig"] = base64.b64encode(b"\x00" * 64).decode()
    assert not await signer.verify(
        ledger=LEDGER_AUDIT,
        sequence=None,
        prev_hash=_PREV,
        entry_hash=_ENTRY,
        envelope=json.dumps(parsed).encode(),
    )


async def test_signing_failure_propagates_rather_than_returning_unsigned() -> None:
    """Fail closed: a KMS outage must abort the write, never yield an unsigned entry."""

    class BrokenKms:
        async def sign(self, ref: object, message: bytes) -> object:
            raise KmsUnavailable("vault unreachable")

    signer = LedgerSigner(BrokenKms())  # type: ignore[arg-type]
    with pytest.raises(KmsUnavailable):
        await signer.sign(ledger=LEDGER_AUDIT, sequence=None, prev_hash=_PREV, entry_hash=_ENTRY)


async def test_a_non_kms_provider_fault_is_wrapped_not_leaked() -> None:
    """A fault outside the KMS taxonomy still fails closed, with a ledger-shaped error."""

    class WeirdKms:
        async def sign(self, ref: object, message: bytes) -> object:
            raise RuntimeError("socket exploded")

    signer = LedgerSigner(WeirdKms())  # type: ignore[arg-type]
    with pytest.raises(LedgerSignatureError, match="ledger signing failed"):
        await signer.sign(ledger=LEDGER_AUDIT, sequence=None, prev_hash=_PREV, entry_hash=_ENTRY)


async def test_the_envelope_is_self_describing() -> None:
    """A future verifier must be able to tell what it is looking at without external context."""
    signature = await _signer().sign(
        ledger=LEDGER_CUSTODY, sequence=7, prev_hash=_PREV, entry_hash=_ENTRY
    )
    parsed = json.loads(signature.envelope)
    assert parsed["v"] == SIGNATURE_ENVELOPE_VERSION
    header = parsed["sigs"][0]
    # Everything needed to rebuild the signed header, and nothing that has to be guessed.
    assert set(header) == {"chv", "sv", "alg", "kid", "kp", "req", "ph", "pha", "ts", "sig"}
    assert header["kp"] == "evidence_root"
    assert header["req"] == ["ED25519"]
