"""The event signer, without a database — ADR-0007, Wave 2.3.

`tests/integration/test_event_signing_db.py` proves the end-to-end claim: a forged outbox row never
reaches a handler. This file covers the primitive underneath it, where every field can be varied one
at a time.

That per-field coverage is the point. A signature that covered only the payload would pass an
end-to-end forgery test while leaving `event_type` rewritable — so each field in the signed message
gets a test that changes *only* it and asserts the signature stops verifying. Real Ed25519 from the
dev KMS provider throughout; a stub would make all of it vacuous.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from sentinelai.platform.crypto.types import KeyPurpose
from sentinelai.platform.events.signing import (
    EVENT_SIGNATURE_VERSION,
    EVENT_SIGNING_KEY,
    EventSignatureError,
    EventSigner,
    signed_event_message,
)
from tests.fixtures.kms import foreign_kms_for_tests, kms_for_tests

_NOW = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)
_SCHEMA = "ingestion"


def _fields(**overrides: object) -> dict[str, object]:
    """One complete signed-message field set, with targeted overrides."""
    base: dict[str, object] = {
        "event_id": UUID(int=1),
        "event_type": "evidence.ingested",
        "event_version": "1.0.0",
        "aggregate_type": "evidence",
        "aggregate_id": UUID(int=2),
        "payload": {"marker": "m", "nested": {"k": [1, True, None]}},
        "correlation_id": UUID(int=3),
        "causation_id": None,
        "trace_id": None,
        "actor_type": "user",
        "actor_ref": UUID(int=4),
        "occurred_at": _NOW,
    }
    base.update(overrides)
    return base


def _signer() -> EventSigner:
    return EventSigner(kms_for_tests())


# ---------------------------------------------------------------------------------------
# The signed message
# ---------------------------------------------------------------------------------------


def test_the_key_is_the_event_root_not_the_evidence_root() -> None:
    """Separate keys by design: an event signature must never read as a custody attestation."""
    assert EVENT_SIGNING_KEY.purpose is KeyPurpose.EVENT_ROOT


def test_the_message_is_canonical_json_carrying_the_version_and_schema() -> None:
    raw = signed_event_message(schema=_SCHEMA, **_fields())  # type: ignore[arg-type]
    decoded = json.loads(raw)

    assert decoded["v"] == EVENT_SIGNATURE_VERSION
    assert decoded["schema"] == _SCHEMA
    # JCS sorts keys, which is what makes the bytes reproducible after a JSONB round trip.
    assert list(decoded) == sorted(decoded)


def test_the_message_is_byte_identical_for_identical_input() -> None:
    """Reproducibility is the whole basis of verification."""
    assert signed_event_message(schema=_SCHEMA, **_fields()) == signed_event_message(  # type: ignore[arg-type]
        schema=_SCHEMA, **_fields()
    )


def test_a_reordered_payload_produces_the_same_message() -> None:
    """Postgres does not preserve JSONB key order, so canonicalization has to absorb it.

    Without this property every signature would break on a round trip that changed nothing.
    """
    first = signed_event_message(schema=_SCHEMA, **_fields(payload={"a": 1, "b": 2}))  # type: ignore[arg-type]
    second = signed_event_message(schema=_SCHEMA, **_fields(payload={"b": 2, "a": 1}))  # type: ignore[arg-type]
    assert first == second


def test_an_absent_causation_id_is_null_not_the_string_none() -> None:
    """JSON ``null`` and ``"None"`` differ under JCS; conflating them changes the signature."""
    decoded = json.loads(signed_event_message(schema=_SCHEMA, **_fields(causation_id=None)))  # type: ignore[arg-type]
    assert decoded["causation_id"] is None


def test_a_naive_timestamp_is_refused() -> None:
    """Guessing a timezone would silently change the signed bytes."""
    with pytest.raises(EventSignatureError, match="timezone-aware"):
        signed_event_message(schema=_SCHEMA, **_fields(occurred_at=datetime(2026, 9, 28, 12, 0)))  # type: ignore[arg-type]


def test_the_timestamp_is_rendered_in_the_wire_form() -> None:
    """The same RFC 3339 ``Z`` form the API emits, so a verifier hashes the bytes it was given."""
    decoded = json.loads(signed_event_message(schema=_SCHEMA, **_fields()))  # type: ignore[arg-type]
    assert decoded["occurred_at"] == "2026-09-28T12:00:00Z"


# ---------------------------------------------------------------------------------------
# Sign / verify
# ---------------------------------------------------------------------------------------


async def test_a_signature_verifies_against_the_fields_it_was_made_over() -> None:
    signer = _signer()
    signature = await signer.sign(schema=_SCHEMA, **_fields())  # type: ignore[arg-type]

    assert "ED25519" in signature.sig_alg
    assert signature.key_id.startswith("dev:")
    assert await signer.verify(schema=_SCHEMA, envelope=signature.envelope, **_fields())  # type: ignore[arg-type]


async def test_a_missing_envelope_does_not_verify() -> None:
    """An unsigned event is not authentic. The dispatcher tells this apart from a forgery itself."""
    assert not await _signer().verify(schema=_SCHEMA, envelope=None, **_fields())  # type: ignore[arg-type]


async def test_a_malformed_envelope_does_not_verify_and_does_not_raise() -> None:
    """It was presented as a signature and does not verify — that is what invalid means.

    Raising would let one corrupt row abort a whole drain instead of quarantining that row.
    """
    assert not await _signer().verify(schema=_SCHEMA, envelope=b"not-an-envelope", **_fields())  # type: ignore[arg-type]


async def test_a_signature_from_another_key_does_not_verify() -> None:
    """Anyone can sign; only the configured EVENT_ROOT key counts."""
    signature = await EventSigner(foreign_kms_for_tests()).sign(schema=_SCHEMA, **_fields())  # type: ignore[arg-type]

    assert not await _signer().verify(schema=_SCHEMA, envelope=signature.envelope, **_fields())  # type: ignore[arg-type]


async def test_a_signature_does_not_verify_in_another_schema() -> None:
    """The schema is a domain separator (ADR-0007 §1): no cross-module transplantation."""
    signer = _signer()
    signature = await signer.sign(schema=_SCHEMA, **_fields())  # type: ignore[arg-type]

    assert not await signer.verify(
        schema="case_management",
        envelope=signature.envelope,
        **_fields(),  # type: ignore[arg-type]
    )


@pytest.mark.parametrize(
    ("field", "tampered"),
    [
        ("event_id", UUID(int=99)),
        ("event_type", "evidence.superseded"),
        ("event_version", "2.0.0"),
        ("aggregate_type", "case"),
        ("aggregate_id", UUID(int=99)),
        ("payload", {"marker": "rewritten"}),
        ("correlation_id", UUID(int=99)),
        ("causation_id", UUID(int=99)),
        ("trace_id", "injected-trace"),
        ("actor_type", "system"),
        ("actor_ref", UUID(int=99)),
        ("occurred_at", _NOW + timedelta(seconds=1)),
    ],
)
async def test_changing_any_signed_field_breaks_the_signature(field: str, tampered: object) -> None:
    """Every field in the signed message is load-bearing, asserted one at a time.

    A signature covering only the payload would still pass an end-to-end forgery test while leaving
    ``event_type`` or ``actor_ref`` freely rewritable — the fields an attacker most wants. This is
    the table that makes "the signature covers the event" a checked statement rather than a claim.
    """
    signer = _signer()
    signature = await signer.sign(schema=_SCHEMA, **_fields())  # type: ignore[arg-type]

    assert not await signer.verify(
        schema=_SCHEMA,
        envelope=signature.envelope,
        **_fields(**{field: tampered}),  # type: ignore[arg-type]
    )


async def test_two_signatures_over_the_same_event_both_verify() -> None:
    """Ed25519 is deterministic, but verification must not depend on that.

    Re-signing the same event is legitimate (a republish after a rollback, say), and both envelopes
    have to verify — a check that compared envelope bytes instead of verifying them would fail here.
    """
    signer = _signer()
    first = await signer.sign(schema=_SCHEMA, **_fields())  # type: ignore[arg-type]
    second = await signer.sign(schema=_SCHEMA, **_fields())  # type: ignore[arg-type]

    assert await signer.verify(schema=_SCHEMA, envelope=first.envelope, **_fields())  # type: ignore[arg-type]
    assert await signer.verify(schema=_SCHEMA, envelope=second.envelope, **_fields())  # type: ignore[arg-type]


async def test_the_recorded_key_id_names_the_provider_and_version() -> None:
    """Operations has to answer "which events used the key version we are retiring?" by query."""
    signature = await _signer().sign(schema=_SCHEMA, **_fields())  # type: ignore[arg-type]

    provider, version, backend_ref = signature.key_id.split(":", 2)
    assert provider == "dev"
    assert version.isdigit()
    assert "event_root" in backend_ref


def test_a_fresh_uuid_cannot_be_smuggled_past_the_message() -> None:
    """ADR-0007 §4: ``event_id`` is inside the signed message.

    The Inbox deduplicates on ``(event_id, handler_name)``, so a forger who mints a fresh id defeats
    it entirely. Binding the id is what makes that detectable independent of inbox state.
    """
    original = signed_event_message(schema=_SCHEMA, **_fields(event_id=UUID(int=1)))  # type: ignore[arg-type]
    replayed = signed_event_message(schema=_SCHEMA, **_fields(event_id=uuid4()))  # type: ignore[arg-type]
    assert original != replayed
