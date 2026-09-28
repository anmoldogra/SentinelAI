"""Event authentication — ADR-0007, modernization Wave 2.3.

**What is being defended against.** An insider who can ``INSERT`` into a schema's
``outbox_events`` can forge domain facts. ``evidence.superseded`` or ``case.status_changed``
written by hand look exactly like the real thing to every consumer, and the Inbox does not help:
it deduplicates on ``(event_id, handler_name)``, and a forger simply mints a fresh ``uuid4``.
Until this module, the event bus authenticated nothing at all.

A detached Ed25519 signature over the event's canonical form closes it, because the signing key
lives in a KMS the database role cannot read. Forging an event then requires something no amount
of ``INSERT`` permission grants.

**What is signed, and what deliberately is not.** The signature covers the publisher's assertion:
the event's identity, type, aggregate, payload, causal triad, actor, and timestamp. It does
**not** cover ``dispatch_status``, ``attempt_count``, ``last_error`` or ``last_attempted_at`` —
those are the relay's bookkeeping, they change legitimately many times after publication, and
signing them would make every retry invalidate the signature. This is the same distinction the
ledger preimage draws between what the writer asserted and what the system later recorded about
it.

``event_id`` *is* inside the signed message (ADR-0007 §4), which is what makes a forged or
replayed id detectable independently of inbox dedup.

**The schema is a domain separator.** ``ingestion.outbox_events`` and
``case_management.outbox_events`` are different tables owned by different modules, and a signature
made for one must not validate a row copied into the other. Including the schema makes
cross-module transplantation detectable even when every other field is identical — the same
reasoning that puts a ledger name in :func:`~sentinelai.platform.crypto.ledger.signed_message`.

**RFC 8785 JCS, not ``json.dumps``.** The payload is JSONB: Postgres does not preserve key order
or whitespace, so a signature over a non-canonical encoding would break on a round trip that
changed nothing. Canonicalization is what makes the signature survive storage and remain
reproducible by an independent verifier — including, later, one on the other side of a Redpanda
topic (ADR-0007 §5).

The signature container is ADR-0009 C1's, shared with the evidentiary ledgers and their anchors,
so a verifier has one envelope format to understand rather than three.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final
from uuid import UUID

from sentinelai.platform.crypto.canonical import canonicalize
from sentinelai.platform.crypto.exceptions import CryptoError
from sentinelai.platform.crypto.kms import KeyManagementService
from sentinelai.platform.crypto.ledger import (
    decode_signature_envelope,
    encode_signature_envelope,
)
from sentinelai.platform.crypto.types import KeyId, KeyPurpose, KeyRef

# ADR-0009 §7 reserves EVENT_ROOT for exactly this ("ADR-0007 outbox signing"). Separate from
# EVIDENCE_ROOT on purpose: an event signature must never be presentable as a custody attestation,
# and rotating one key must not force rotating the other.
EVENT_SIGNING_KEY: Final = KeyRef(purpose=KeyPurpose.EVENT_ROOT, name="default")

# Version of the *field set* that goes into a signed event message. Distinct from `event_version`,
# which versions the payload contract. Bump only when the signed field list changes, so a verifier
# meeting an older row knows which fields it has to reconstruct.
EVENT_SIGNATURE_VERSION: Final = 1


class EventSignatureError(CryptoError):
    """An event signature could not be produced or parsed.

    Distinct from a signature that verifies *false*: this means the operation could not complete at
    all. A publisher must treat it as fatal: an unsigned event is an unauthenticated one, and
    writing it would put a fact on the bus that no one can attribute.
    """


@dataclass(frozen=True, slots=True)
class EventSignature:
    """What gets persisted onto an outbox row.

    ``sig_alg`` and ``key_id`` duplicate what is already inside ``envelope``. They are columns
    because operations has to answer "which events were signed under the key version we are
    retiring?" with a query rather than by parsing every row. They are **not** trusted during
    verification — the envelope's own copies are, and those are covered by the signature.
    """

    envelope: bytes
    sig_alg: str
    key_id: str


def _serialize_key_id(key_id: KeyId) -> str:
    """``provider:version:backend_ref`` — the same rendering the ledger columns use."""
    return f"{key_id.provider.value}:{key_id.version}:{key_id.backend_ref}"


def _render_uuid(value: UUID | None) -> str | None:
    """``None`` stays ``None`` rather than becoming ``"None"``.

    JSON ``null`` and the string ``"null"`` are distinct under JCS, so an absent causation id cannot
    be confused with one whose value happens to be that text.
    """
    return str(value) if value is not None else None


def signed_event_message(
    *,
    schema: str,
    event_id: UUID,
    event_type: str,
    event_version: str,
    aggregate_type: str,
    aggregate_id: UUID,
    payload: dict[str, Any],
    correlation_id: UUID,
    causation_id: UUID | None,
    trace_id: str | None,
    actor_type: str,
    actor_ref: UUID | None,
    occurred_at: datetime,
) -> bytes:
    """The exact bytes an event signature covers.

    Keyword-only, because this takes twelve values of which several are ``str | None`` and a
    positional call that transposed ``actor_type`` and ``trace_id`` would still typecheck, still
    sign, and produce a signature that verifies against nothing.

    ``occurred_at`` is rendered in the same RFC 3339 ``Z`` form the API puts on the wire, for the
    reason ``ledger.ledger_timestamp`` documents: a verifier should hash exactly the bytes it was
    given rather than translating six characters of timezone offset first. A naive
    datetime is refused rather than assumed to be UTC.
    """
    if occurred_at.tzinfo is None:
        raise EventSignatureError(
            "an event timestamp must be timezone-aware; a naive datetime has no canonical form"
        )
    return canonicalize(
        {
            "v": EVENT_SIGNATURE_VERSION,
            # Domain separator: a signature for one module's outbox must not validate in another's.
            "schema": schema,
            "event_id": str(event_id),
            "event_type": event_type,
            "event_version": event_version,
            "aggregate_type": aggregate_type,
            "aggregate_id": str(aggregate_id),
            "payload": payload,
            "correlation_id": str(correlation_id),
            "causation_id": _render_uuid(causation_id),
            "trace_id": trace_id,
            "actor_type": actor_type,
            "actor_ref": _render_uuid(actor_ref),
            "occurred_at": occurred_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        }
    )


class EventSigner:
    """Signs and verifies outbox events under ``KeyPurpose.EVENT_ROOT`` — ADR-0007 §1/§2.

    **Signing fails closed.** If the KMS is unreachable, :meth:`sign` raises and the publisher's
    transaction aborts with it. That is deliberate, and the same trade ADR-0003 §1 makes for the
    ledgers: the business write and the outbox row share one transaction (event-driven §16), so
    refusing to sign refuses the whole operation rather than committing a fact the bus cannot
    authenticate. An unsigned event is not a degraded event — under strict verification it is an
    event that will never be delivered, which is worse than a visible failure at publish time.
    """

    def __init__(self, kms: KeyManagementService, *, key: KeyRef = EVENT_SIGNING_KEY) -> None:
        self._kms = kms
        self._key = key

    async def sign(self, *, schema: str, **fields: Any) -> EventSignature:
        """Sign one event. ``fields`` are :func:`signed_event_message`'s keyword arguments."""
        message = signed_event_message(schema=schema, **fields)
        try:
            bundle = await self._kms.sign(self._key, message)
        except CryptoError:
            raise
        except Exception as exc:  # a provider fault that escaped the KMS error taxonomy
            raise EventSignatureError(f"event signing failed: {exc}") from exc
        return EventSignature(
            envelope=encode_signature_envelope(bundle),
            # Every algorithm in the bundle, so a hybrid (PQC) bundle stays queryable by either.
            sig_alg=",".join(sorted(s.header.algorithm.value for s in bundle.signatures)),
            key_id=_serialize_key_id(bundle.primary.header.key_id),
        )

    async def verify(self, *, schema: str, envelope: bytes | None, **fields: Any) -> bool:
        """Whether ``envelope`` is a valid signature over this event.

        ``False`` for a missing envelope: an unsigned event is not authentic. A caller that needs to
        tell "unsigned" from "forged" apart — and the dispatcher does, because the two get different
        treatment under permissive verification — must check ``envelope is None`` itself.

        A malformed envelope also returns ``False`` rather than raising: it was presented as a
        signature and it does not verify, which is what invalid means.
        """
        if envelope is None:
            return False
        message = signed_event_message(schema=schema, **fields)
        try:
            bundle = decode_signature_envelope(envelope)
        except CryptoError:
            return False
        return await self._kms.verify(message, bundle)


__all__ = [
    "EVENT_SIGNATURE_VERSION",
    "EVENT_SIGNING_KEY",
    "EventSignature",
    "EventSignatureError",
    "EventSigner",
    "signed_event_message",
]
