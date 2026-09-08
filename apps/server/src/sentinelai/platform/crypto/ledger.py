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

**Scope, stated plainly: nothing here signs.** ADR-0003 §1 requires a signature over
``(sequence || prev_entry_hash || entry_hash)`` from a KMS key the application's database role
cannot read, and that is what actually closes PRD SR-4 against a privileged writer. This module
produces the ``entry_hash`` such a signature will cover; ``sig_alg``, ``key_id``, and
``signature`` remain null until the signing increment lands. A complete preimage narrows what a
forger can rewrite silently; it does not stop one who can recompute the whole chain.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Final
from uuid import UUID

from sentinelai.platform.crypto.canonical import canonicalize

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


__all__ = [
    "LEDGER_HASH_ALGO",
    "LEDGER_PREIMAGE_VERSION",
    "LedgerPreimageError",
    "compute_entry_hash",
    "ledger_timestamp",
    "ledger_uuid",
]
