"""Evidentiary ledger entry hashing — ADR-0003 §1/§2/§5, modernization Wave 1.2.

The one place an entry hash is computed, shared by both ledgers ``platform.audit_log`` and
``ingestion.evidence_custody_events``. They are deliberately not two implementations: ADR-0003
treats them as one integrity subsystem, and the Verification Engine (Wave 1.4) must be able to
re-derive either with the same code. Two copies of "canonicalize, then SHA-256" would drift, and
the drift would only surface years later as an unverifiable chain.

**What this module fixes.** Before Wave 1.2 both ledgers hashed a *partial* preimage with
``json.dumps`` — audit covered only ``{prev, action, target_id, details}``, custody only six of
its eleven columns. ADR-0003 Context §2 names the consequence exactly: "the forgeable fields are
exactly the attribution fields". Who did it, in what role, from what address, under what legal
authority — none of it was bound to the hash, so any of it could be rewritten without breaking
the chain. This module makes the preimage complete and the encoding canonical (RFC 8785 JCS, so
a JSONB round-trip cannot change the bytes).

**The agility metadata is inside the hash, not beside it.** ``hash_algo`` and
``preimage_version`` are injected into every preimage by :func:`compute_entry_hash` rather than
being left to callers. That is a downgrade defense, and it is the same reasoning
``crypto.types.SignedHeader`` applies to ``required_algorithms``: if the version that says *how to
verify* were merely stored next to the entry, an attacker could rewrite an entry under the old
incomplete format, set ``preimage_version`` back, and have it verify. Because the version is
covered, changing it changes the hash.

**Signing (ADR-0003 §1) lives here too**, in :class:`LedgerSigner`. That is what actually closes
PRD SR-4: a complete preimage catches an attacker who edits one row, but it does nothing against
one who can recompute the whole chain — nothing about a hash requires a secret. A signature made
under a KMS key the application's database role cannot read does, because rewriting history then
needs something no amount of database access grants.

Hashing and signing sit in one module deliberately. They are two halves of one operation: the
signature covers the hash, and the Verification Engine (Wave 1.4) must check both or neither. A
verifier that confirmed the hash and skipped the signature would report a forged chain as intact.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final
from uuid import UUID

from sentinelai.platform.crypto.canonical import canonicalize
from sentinelai.platform.crypto.exceptions import CryptoError
from sentinelai.platform.crypto.kms import KeyManagementService
from sentinelai.platform.crypto.types import (
    Algorithm,
    KeyId,
    KeyPurpose,
    KeyRef,
    ProviderKind,
    Signature,
    SignatureBundle,
    SignedHeader,
)

# Version of the **field set** that goes into a preimage — not of the encoding, which
# `canonical.CANONICAL_ENCODING` names separately (ADR-0003 §5 keeps the two axes independent).
#
# 1 is the first *complete* preimage. Rows written before Wave 1.2 carry NULL, which is not
# version 0 and must not be treated as one: they were hashed over a partial field set with a
# non-canonical encoder, so they cannot be re-derived by this module at all. A verifier that
# meets a NULL must report the entry as not independently verifiable rather than as failed —
# those are different findings, and on a custody surface only one of them is honest.
LEDGER_PREIMAGE_VERSION: Final = 1

# Digest for `entry_hash`. ADR-0003's adopted default; recorded on every row so a later move to
# SHA-384 leaves history verifiable under the algorithm it was actually written with.
LEDGER_HASH_ALGO: Final = "SHA-256"

_HASHLIB_NAMES: Final[dict[str, str]] = {"SHA-256": "sha256", "SHA-384": "sha384"}

# Injected by `compute_entry_hash`; a caller supplying them itself is a bug, because it would
# mean the covered version could disagree with the stored one.
_RESERVED_KEYS: Final = frozenset({"hash_algo", "preimage_version"})


class LedgerPreimageError(ValueError):
    """A ledger preimage could not be built or hashed.

    Raised, never swallowed. An audit or custody write that cannot be hashed must fail its whole
    transaction: a ledger entry that exists but is not covered by a usable hash is worse than a
    refused write, because it looks like evidence and verifies as nothing.
    """


def ledger_timestamp(value: datetime) -> str:
    """Render a timestamp for a preimage, in the **same form the API puts on the wire**.

    RFC 3339 with a ``Z`` suffix, normalized to UTC — ``2026-09-08T12:00:00.123456Z``.

    This deliberately differs from the pre-Wave-1.2 preimage, which hashed
    ``datetime.isoformat()`` and therefore ``+00:00``. Pydantic serializes the same value with
    ``Z`` (api-design.md §2.3 mandates that form), so the string the server hashed was *not* the
    string the client received, and every independent verifier had to know to translate those six
    characters back. Hashing the wire form removes that entire class of defect: a caller hashes
    exactly the bytes it was given.

    A naive datetime is rejected rather than assumed to be UTC. Guessing a timezone would silently
    change a preimage, and on a custody record the timestamp is evidence in its own right.
    """
    if value.tzinfo is None:
        raise LedgerPreimageError(
            "a ledger timestamp must be timezone-aware; a naive datetime has no canonical form"
        )
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def ledger_uuid(value: UUID | None) -> str | None:
    """Render a UUID (or its absence) for a preimage.

    ``None`` stays ``None`` rather than becoming ``"None"`` or ``""``: JSON ``null`` and the
    string ``"null"`` are distinct in JCS, so an absent actor cannot be confused with one whose
    identifier happens to be that text.
    """
    return None if value is None else str(value)


def compute_entry_hash(fields: Mapping[str, object]) -> str:
    """Hash one ledger entry: SHA-256 over the JCS encoding of ``fields`` + agility metadata.

    ``fields`` must already be JSON-shaped (see :func:`ledger_timestamp`, :func:`ledger_uuid`).
    Anything RFC 8785 cannot represent raises rather than being coerced — see
    ``crypto.canonical`` for why silent coercion is the one failure mode this subsystem exists to
    prevent.
    """
    overlap = _RESERVED_KEYS.intersection(fields)
    if overlap:
        raise LedgerPreimageError(
            f"{sorted(overlap)} are injected by compute_entry_hash and must not be supplied by "
            "the caller — a caller-supplied version could disagree with the stored one"
        )
    preimage: dict[str, object] = dict(fields)
    preimage["hash_algo"] = LEDGER_HASH_ALGO
    preimage["preimage_version"] = LEDGER_PREIMAGE_VERSION
    return hashlib.new(_HASHLIB_NAMES[LEDGER_HASH_ALGO], canonicalize(preimage)).hexdigest()


# ---------------------------------------------------------------------------------------
# Signing — ADR-0003 §1
# ---------------------------------------------------------------------------------------

# The logical key both evidentiary ledgers sign under. ADR-0009 §7 reserves EVIDENCE_ROOT for
# exactly this ("ADR-0003 custody/audit signing + Merkle roots"), so custody and audit share one
# key: they are one integrity subsystem, and a verifier should not have to discover which of two
# roots covers a given entry.
EVIDENCE_LEDGER_KEY: Final = KeyRef(purpose=KeyPurpose.EVIDENCE_ROOT, name="default")

# Envelope format version for the bytes stored in the `signature` column. Distinct from
# `preimage_version` (which field set was hashed) and from the signature/header versions inside
# the envelope (ADR-0009's own agility metadata). Bump only on a breaking change to this container.
SIGNATURE_ENVELOPE_VERSION: Final = 1

# Ledger discriminators, present in every signed message so a signature made for one ledger can
# never be replayed onto the other even if the remaining fields coincided.
LEDGER_AUDIT: Final = "platform.audit_log"
LEDGER_CUSTODY: Final = "ingestion.evidence_custody_events"


class LedgerSignatureError(CryptoError):
    """A ledger signature could not be produced or parsed.

    Distinct from a signature that verifies *false*: this means the operation could not be
    completed at all. Callers must treat both as failure — an entry is written signed or it is not
    written — but only one of them means "the ledger is wrong".
    """


@dataclass(frozen=True, slots=True)
class LedgerSignature:
    """What gets persisted onto a ledger row.

    ``envelope`` is the whole self-authenticating bundle (ADR-0009 C1) and is the only field
    verification actually needs. ``sig_alg`` and ``key_id`` duplicate what is already inside it,
    because operations has to answer "which entries were signed under the key version we are
    retiring?" with a query rather than by parsing every row — and because ADR-0003 §5 names them
    as columns. They are **not** trusted during verification: the envelope's own copies are, and
    those are covered by the signature.
    """

    envelope: bytes
    sig_alg: str
    key_id: str


def _serialize_key_id(key_id: KeyId) -> str:
    """``provider:version:backend_ref`` — the format ``platform.auth.repository`` already uses.

    The backend ref goes last and is parsed with ``split(":", 2)`` because it is provider-shaped
    and may itself contain colons (an AWS KMS ARN is the obvious case).
    """
    return f"{key_id.provider.value}:{key_id.version}:{key_id.backend_ref}"


def _bundle_to_envelope(bundle: SignatureBundle) -> bytes:
    """Serialize a signature bundle to canonical JSON.

    The per-signature keys mirror ``SignedHeader.canonical_bytes()`` exactly, so the header can be
    rebuilt field for field on the way back. Anything lost here is a signature that can never be
    verified again, which is why ``created_at`` and the required-algorithm set are stored rather
    than recomputed: the timestamp is captured at signing time and is not derivable afterwards, and
    the required set reflects the policy in force *then*, not now.
    """
    return canonicalize(
        {
            "v": SIGNATURE_ENVELOPE_VERSION,
            "sigs": [
                {
                    "chv": s.header.canonical_header_version,
                    "sv": s.header.signature_version,
                    "alg": s.header.algorithm.value,
                    "kid": {
                        "p": s.header.key_id.provider.value,
                        "r": s.header.key_id.backend_ref,
                        "v": s.header.key_id.version,
                    },
                    "kp": s.header.key_purpose.value,
                    "req": sorted(a.value for a in s.header.required_algorithms),
                    "ph": s.header.payload_hash,
                    "pha": s.header.payload_hash_algorithm,
                    "ts": s.header.created_at,
                    "sig": base64.b64encode(s.value).decode(),
                }
                for s in bundle.signatures
            ],
        }
    )


def _envelope_to_bundle(envelope: bytes) -> SignatureBundle:
    """Rebuild a signature bundle from stored bytes. Raises rather than returning a partial one."""
    try:
        parsed = json.loads(envelope.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LedgerSignatureError("signature envelope is not valid JSON") from exc
    if not isinstance(parsed, dict):
        raise LedgerSignatureError("signature envelope is not a JSON object")
    if parsed.get("v") != SIGNATURE_ENVELOPE_VERSION:
        raise LedgerSignatureError("unsupported signature envelope version")
    raw_signatures = parsed.get("sigs")
    if not isinstance(raw_signatures, list) or not raw_signatures:
        raise LedgerSignatureError("signature envelope carries no signatures")
    try:
        signatures = tuple(
            Signature(
                header=SignedHeader(
                    canonical_header_version=int(s["chv"]),
                    signature_version=int(s["sv"]),
                    algorithm=Algorithm(s["alg"]),
                    key_id=KeyId(
                        provider=ProviderKind(s["kid"]["p"]),
                        backend_ref=str(s["kid"]["r"]),
                        version=int(s["kid"]["v"]),
                    ),
                    key_purpose=KeyPurpose(s["kp"]),
                    required_algorithms=tuple(Algorithm(a) for a in s["req"]),
                    payload_hash=str(s["ph"]),
                    payload_hash_algorithm=str(s["pha"]),
                    created_at=str(s["ts"]),
                ),
                value=base64.b64decode(s["sig"], validate=True),
            )
            for s in raw_signatures
        )
    except (KeyError, TypeError, ValueError, binascii.Error) as exc:
        raise LedgerSignatureError(f"malformed signature envelope: {exc}") from exc
    return SignatureBundle(signatures)


def signed_message(*, ledger: str, sequence: int | None, prev_hash: str, entry_hash: str) -> bytes:
    """The bytes a ledger entry's signature covers — ADR-0003 §1.

    The ADR writes this as ``(sequence || prev_entry_hash || entry_hash)``. It is built here as a
    **canonical JSON object rather than a raw concatenation**, for one reason worth stating: under
    crypto agility, hash widths are not fixed. A SHA-256 digest is 64 hex characters and a SHA-384
    digest is 96, so ``prev || entry`` is unambiguous only while every entry uses one algorithm.
    Concatenating variable-length fields is the classic way to let two different tuples produce one
    signed byte string. JCS removes that ambiguity at no cost and keeps the signed bytes
    reproducible by an independent verifier — the same reason the entry hash uses it.

    ``ledger`` is a domain separator. Without it, a signature made over one ledger's entry could in
    principle be presented as a valid signature for the other's, should the remaining fields ever
    coincide.

    ``sequence`` is ``None`` for ``platform.audit_log``, which has no sequence column — its
    ordering is the chain itself. That is not a gap: ``entry_hash`` already covers ``audit_id`` and
    every other persisted field, so the entry's identity is bound regardless.
    """
    return canonicalize(
        {"ledger": ledger, "seq": sequence, "prev": prev_hash, "entry_hash": entry_hash}
    )


class LedgerSigner:
    """Signs and verifies evidentiary ledger entries under ``KeyPurpose.EVIDENCE_ROOT``.

    This is the boundary ADR-0003 §1 is really about. A complete preimage (Wave 1.2) catches an
    attacker who edits one row; it does nothing against one who can recompute the whole chain. A
    signature does, because the key lives in a KMS/HSM the application's database role cannot read
    — so rewriting history requires something no amount of database access grants.

    **Fails closed, always.** If the KMS is unreachable, signing raises and the caller's
    transaction aborts. An unsigned entry is not a degraded entry; it is a gap in the evidence that
    no later process can fill, because the bytes that should have been signed are gone by then.
    Writing one would trade a visible outage for an invisible hole in a legal record.
    """

    def __init__(self, kms: KeyManagementService, *, key: KeyRef = EVIDENCE_LEDGER_KEY) -> None:
        self._kms = kms
        self._key = key

    async def sign(
        self, *, ledger: str, sequence: int | None, prev_hash: str, entry_hash: str
    ) -> LedgerSignature:
        message = signed_message(
            ledger=ledger, sequence=sequence, prev_hash=prev_hash, entry_hash=entry_hash
        )
        try:
            bundle = await self._kms.sign(self._key, message)
        except CryptoError:
            raise
        except Exception as exc:  # a provider fault that escaped the KMS error taxonomy
            raise LedgerSignatureError(f"ledger signing failed: {exc}") from exc
        return LedgerSignature(
            envelope=_bundle_to_envelope(bundle),
            # Every algorithm in the bundle, so a hybrid (PQC) bundle is queryable by either.
            sig_alg=",".join(sorted(s.header.algorithm.value for s in bundle.signatures)),
            key_id=_serialize_key_id(bundle.primary.header.key_id),
        )

    async def verify(
        self,
        *,
        ledger: str,
        sequence: int | None,
        prev_hash: str,
        entry_hash: str,
        envelope: bytes | None,
    ) -> bool:
        """Return whether ``envelope`` is a valid signature over this entry.

        ``False`` for a missing envelope: an unsigned entry is not authentic, and a verifier that
        returned ``True`` for one would report the pre-signing history as proven. Callers needing to
        distinguish "unsigned" from "forged" must check ``envelope is None`` themselves — the
        Verification Engine (Wave 1.4) does, because on a court-facing report those are different
        findings.

        A malformed envelope also returns ``False`` rather than raising: it was presented as a
        signature and it does not verify, which is the definition of an invalid one.
        """
        if envelope is None:
            return False
        message = signed_message(
            ledger=ledger, sequence=sequence, prev_hash=prev_hash, entry_hash=entry_hash
        )
        try:
            bundle = _envelope_to_bundle(envelope)
        except LedgerSignatureError:
            return False
        return await self._kms.verify(message, bundle)


__all__ = [
    "EVIDENCE_LEDGER_KEY",
    "LEDGER_AUDIT",
    "LEDGER_CUSTODY",
    "LEDGER_HASH_ALGO",
    "LEDGER_PREIMAGE_VERSION",
    "SIGNATURE_ENVELOPE_VERSION",
    "LedgerPreimageError",
    "LedgerSignature",
    "LedgerSignatureError",
    "LedgerSigner",
    "compute_entry_hash",
    "ledger_timestamp",
    "ledger_uuid",
    "signed_message",
]
