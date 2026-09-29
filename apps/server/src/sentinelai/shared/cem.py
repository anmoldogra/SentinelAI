"""Canonical Evidence Model value objects — ADR-0011 §2, modernization Wave 2.4.

**What a value object buys here.** Every one of these concepts was previously a bare ``str`` or
``Decimal`` validated, if at all, at whichever call site happened to remember. A confidence of
``1.7`` or an integrity hash of ``"probably sha256 of something"`` was constructible and would only
fail — if ever — much later, in a context with no idea what the right value should have been. Each
type below validates **at construction**, so an invalid instance does not exist to be passed on.

That is the whole claim of ADR-0011 §2, and it is worth being precise about what it is not: these do
not replace the database columns, and they do not replace Pydantic request validation at the API
edge. They are the domain's own vocabulary, usable by an aggregate method that has no request and no
session in scope — which is exactly where the invariants that matter are enforced.

**Why ``shared`` rather than ``modules/ingestion``.** The canonical evidence model is explicitly a
cross-domain contract (the repository map calls ``packages/evidence-schema`` "shared across all
domains"), and two modules already need this vocabulary: ``ingestion`` for evidence, and
``investigation`` for entity/relationship confidence. Putting it in ``ingestion`` would make
``investigation`` depend on another module to name a number between zero and one. ``shared`` is the
lowest layer in the import DAG, so every module can use it and none is coupled to another.

``platform`` *could* import from here, and should not need to: these carry no behaviour that would
make the platform layer domain-aware. Nothing in ``platform`` imports them today.

Every type is frozen and slotted. They are values — two ``ConfidenceScore(0.8)`` instances are the
same thing, and a value object that could be mutated after validation would defeat the point of
validating at construction.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Final

from sentinelai.shared.exceptions import ValidationFailedError

# ---------------------------------------------------------------------------------------
# Vocabularies. Single source of truth for values the database also constrains — the point of
# ADR-0011 §4's "belt and suspenders" is that both layers hold, not that one is redundant.
# ---------------------------------------------------------------------------------------

# CEM §13's permitted integrity algorithms. SHA-1 and MD5 are absent deliberately: an integrity
# hash an attacker can collide is not an integrity hash, and permitting one "for legacy data" would
# mean the column can no longer be trusted uniformly.
INTEGRITY_ALGORITHMS: Final[frozenset[str]] = frozenset({"SHA-256", "SHA-3-256", "SHA-512"})

# Digest length in hex characters, per algorithm. Checked because a 64-character value labelled
# SHA-512 is not a truncated SHA-512 — it is a SHA-256 with the wrong label, and a verifier that
# trusted the label would recompute the wrong thing forever.
_DIGEST_HEX_LENGTHS: Final[dict[str, int]] = {
    "SHA-256": 64,
    "SHA-3-256": 64,
    "SHA-512": 128,
}

# Case-insensitive lookup for :meth:`IntegrityHash.parse`. Derived from the set above rather than
# written out, so a new permitted algorithm cannot be accepted by one and rejected by the other.
_ALGORITHM_BY_CASEFOLD: Final[dict[str, str]] = {
    name.casefold(): name for name in INTEGRITY_ALGORITHMS
}

# CEM §4's custody event-type enum.
CUSTODY_EVENT_TYPES: Final[frozenset[str]] = frozenset(
    {
        "collected",
        "ingested",
        "accessed",
        "exported",
        "transferred",
        "analyzed",
        "integrity_reverified",
        "linked_to_case",
        "unlinked_from_case",
        "legal_hold_applied",
        "legal_hold_released",
        "disposed",
    }
)

# The two custody events that carry a legal-hold state transition, named here so an aggregate does
# not have to hard-code string comparisons to decide what a hold currently is (ADR-0015: the ledger
# is the state).
CUSTODY_LEGAL_HOLD_APPLIED: Final = "legal_hold_applied"
CUSTODY_LEGAL_HOLD_RELEASED: Final = "legal_hold_released"
# The disposal path security-architecture.md §39 requires a legal-hold check on.
CUSTODY_DISPOSED: Final = "disposed"

_HEX = re.compile(r"\A[0-9a-f]+\Z")
# Slug shape for category/artifact_type: lowercase, digits, underscores. Matches the seeded
# attribute-schema registry vocabulary rather than inventing a looser one.
_SLUG = re.compile(r"\A[a-z0-9]+(?:_[a-z0-9]+)*\Z")
_MAX_SLUG_LENGTH: Final = 50
# CEM's sentinel for "lawfully collected from a public source, no instrument required". The literal
# is CEM §13's, verbatim — its validation table and both worked examples (§13, and the OSINT/social
# examples) spell it this way, `apps/web` hardcodes the same string, and the ingest validator has
# always accepted exactly this. It is a wire value in an evidentiary record, so it is quoted from
# the model rather than restyled.
#
# A module constant rather than a class attribute on `LegalAuthorityRef`: a `Final` class var inside
# a `slots=True` dataclass is a slot/class-variable conflict, and the value is vocabulary anyway.
PUBLIC_SOURCE_AUTHORITY: Final = "public_source_no_authority_required"

# `legal_authority_ref` is a free-text citation (a warrant number, a statutory basis). Bounded
# because it lands in an evidentiary record and an unbounded field on a legal citation invites a
# pasted document.
_MAX_AUTHORITY_REF_LENGTH: Final = 500


def _reject(field: str, message: str) -> ValidationFailedError:
    """A 422 carrying the field name, so an API caller learns which value was wrong."""
    return ValidationFailedError([{"field": field, "message": message}])


@dataclass(frozen=True, slots=True)
class IntegrityHash:
    """A payload digest and the algorithm that produced it — CEM §13.

    Validates three things together, because any one alone is insufficient: the algorithm is one
    this platform accepts, the digest is lowercase hex, and its **length matches the algorithm**.
    That last check is the one a hand-rolled validator usually omits, and it is the one that catches
    a SHA-256 digest labelled SHA-512 — a mislabelled digest verifies against nothing forever, and
    nothing downstream would ever notice because the label is what a verifier trusts.
    """

    algorithm: str
    digest: str

    def __post_init__(self) -> None:
        if self.algorithm not in INTEGRITY_ALGORITHMS:
            raise _reject(
                "integrity_algorithm",
                f"unsupported algorithm '{self.algorithm}'; permitted: "
                f"{sorted(INTEGRITY_ALGORITHMS)}",
            )
        if not _HEX.match(self.digest):
            raise _reject(
                "integrity_hash", "digest must be lowercase hexadecimal with no separators"
            )
        expected = _DIGEST_HEX_LENGTHS[self.algorithm]
        if len(self.digest) != expected:
            raise _reject(
                "integrity_hash",
                f"{self.algorithm} produces {expected} hex characters, got {len(self.digest)} — "
                "a digest whose length disagrees with its label is mislabelled, not truncated",
            )

    @classmethod
    def parse(cls, value: str, *, field: str = "integrity_hash") -> IntegrityHash:
        """Parse the ``ALGORITHM:digest`` form :meth:`__str__` produces. The exact inverse.

        Exists because some records carry a digest in **one** column and still have to state which
        algorithm produced it — `forensics.artifacts.acquisition_hash` is the case that needed it
        (`database-design.md` §3.3 gives it no algorithm column, and a 64-character digest is a
        SHA-256 *or* a SHA-3-256, so length cannot answer it). A self-describing value keeps the
        label attached to the digest instead of inferring one, and inferring the wrong label on an
        evidentiary integrity field would make the hash verify against nothing forever.

        Lives here rather than in the module that needed it because the format is this value
        object's own rendering: two places that both know how to write it and only one that knows
        how to read it is how a format drifts.

        The algorithm is matched case-insensitively (`sha-256` is the same algorithm as `SHA-256`)
        and normalized to CEM §13's spelling; the digest is not, because a digest is lowercase hex
        by rule and silently down-casing a caller's value would hide a tool emitting upper case.
        """
        algorithm, separator, digest = value.partition(":")
        if not separator:
            raise _reject(
                field,
                "must be '<ALGORITHM>:<hexdigest>' so the digest states which algorithm produced "
                f"it; permitted algorithms: {sorted(INTEGRITY_ALGORITHMS)}",
            )
        canonical = _ALGORITHM_BY_CASEFOLD.get(algorithm.strip().casefold(), algorithm.strip())
        try:
            return cls(algorithm=canonical, digest=digest.strip())
        except ValidationFailedError as exc:
            # Re-point the 422 at the field the caller actually sent, so an examiner is told
            # `acquisition_hash` is wrong rather than a field name from ingestion's vocabulary.
            raise ValidationFailedError(
                [{"field": field, "message": detail["message"]} for detail in exc.details]
            ) from exc

    def matches(self, other: IntegrityHash) -> bool:
        """Whether two hashes assert the same thing.

        Compares the algorithm too. Two equal digest strings under different algorithms are not a
        match — they are a collision claim nobody made.
        """
        return self.algorithm == other.algorithm and self.digest == other.digest

    def __str__(self) -> str:
        return f"{self.algorithm}:{self.digest}"


@dataclass(frozen=True, slots=True)
class ConfidenceScore:
    """A confidence in ``[0, 1]`` — CEM §13, used by evidence and by AI findings.

    Stored as ``Decimal`` rather than ``float`` because the database column is ``Numeric`` and
    because a confidence is compared and sometimes summed; binary floating point would make
    ``0.1 + 0.2 <= 0.3`` false and a boundary check unreliable at exactly the boundary.
    """

    value: Decimal

    def __post_init__(self) -> None:
        if not isinstance(self.value, Decimal):
            raise _reject("confidence", "confidence must be a Decimal, not a float")
        if self.value.is_nan():
            raise _reject("confidence", "confidence must be a number")
        if not (Decimal("0") <= self.value <= Decimal("1")):
            raise _reject("confidence", f"confidence must be within [0, 1], got {self.value}")

    @classmethod
    def parse(cls, raw: str | int | Decimal) -> ConfidenceScore:
        """Build from whatever the wire supplied, refusing anything unparseable.

        Accepts ``str`` and ``int`` but never ``float``: ``Decimal(0.7)`` is
        ``0.6999999999999999555910790149937383830547332763671875``, which is not what the caller
        said and would be stored and compared as such.
        """
        if isinstance(raw, float):  # pragma: no cover - refused by the type checker too
            raise _reject("confidence", "pass a Decimal or a string, never a float")
        try:
            return cls(Decimal(raw))
        except (InvalidOperation, ValueError) as exc:
            raise _reject("confidence", f"'{raw}' is not a number") from exc

    def __str__(self) -> str:
        return str(self.value)


@dataclass(frozen=True, slots=True)
class EvidenceCategory:
    """A CEM category slug (``osint``, ``forensics``, ...).

    Deliberately **not** a closed enum. The category vocabulary is extended by registering an
    attribute schema (``ingestion.attribute_schema_registry``), so a hard-coded enum here would
    make adding a category a code change and a deployment — and would silently reject data a
    correctly-registered connector is entitled to send. What is validated is the *shape*, so a
    category cannot be an empty string, a sentence, or a value that differs from another only by
    case.
    """

    value: str

    def __post_init__(self) -> None:
        _validate_slug("category", self.value)

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class ArtifactType:
    """A CEM artifact-type slug (``web_page``, ``disk_image``, ...). Open vocabulary, as above."""

    value: str

    def __post_init__(self) -> None:
        _validate_slug("artifact_type", self.value)

    def __str__(self) -> str:
        return self.value


def _validate_slug(field: str, value: str) -> None:
    if not value:
        raise _reject(field, f"{field} must not be empty")
    if len(value) > _MAX_SLUG_LENGTH:
        raise _reject(field, f"{field} must be at most {_MAX_SLUG_LENGTH} characters")
    if not _SLUG.match(value):
        raise _reject(
            field,
            f"{field} must be lowercase alphanumeric words separated by single underscores "
            f"(got '{value}')",
        )


@dataclass(frozen=True, slots=True)
class LegalAuthorityRef:
    """The legal basis under which evidence was collected — CEM §13.

    Required for certain categories, and the reason this is a type rather than a ``str | None`` is
    the sentinel: CEM §13 permits ``"public_source_no_authority_required"`` to mean "lawfully
    collected from a public source, no instrument required". A bare string makes that
    indistinguishable from a caller who typed something meaningless into a mandatory field, so
    :attr:`is_public_source` names it explicitly.

    Whitespace-only is refused. An authority reference of ``"   "`` satisfies a ``not None`` check
    while asserting nothing, which is the failure mode this type exists to remove.
    """

    value: str

    def __post_init__(self) -> None:
        if not self.value or not self.value.strip():
            raise _reject(
                "legal_authority_ref",
                "a legal authority reference must not be empty or whitespace — use "
                f"'{PUBLIC_SOURCE_AUTHORITY}' for lawfully-collected public material",
            )
        if len(self.value) > _MAX_AUTHORITY_REF_LENGTH:
            raise _reject(
                "legal_authority_ref",
                f"must be at most {_MAX_AUTHORITY_REF_LENGTH} characters",
            )

    @property
    def is_public_source(self) -> bool:
        """Whether this is the CEM public-source sentinel rather than a real instrument."""
        return self.value == PUBLIC_SOURCE_AUTHORITY

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class CustodyEventType:
    """One of CEM §4's custody event types.

    A **closed** vocabulary, unlike category and artifact type, and the difference is deliberate:
    every custody event type has specific meaning to the chain-of-custody rules (a ``disposed`` is
    gated on legal hold, a ``legal_hold_applied`` *is* a state transition), so an unrecognised one
    is not extensibility — it is an event nothing knows how to reason about sitting in a legal
    record.
    """

    value: str

    def __post_init__(self) -> None:
        if self.value not in CUSTODY_EVENT_TYPES:
            raise _reject("event_type", f"unknown custody event '{self.value}'")

    @property
    def is_disposal(self) -> bool:
        """Whether this event disposes of evidence — the path §39 gates on legal hold."""
        return self.value == CUSTODY_DISPOSED

    @property
    def legal_hold_state(self) -> bool | None:
        """The hold state this event asserts, or ``None`` if it asserts nothing about holds.

        Three-valued on purpose. ``False`` means "this event releases the hold"; ``None`` means
        "this event says nothing about holds and must leave the current state alone". Collapsing
        them to a boolean would make every ``accessed`` event silently release a legal hold.
        """
        if self.value == CUSTODY_LEGAL_HOLD_APPLIED:
            return True
        if self.value == CUSTODY_LEGAL_HOLD_RELEASED:
            return False
        return None

    def __str__(self) -> str:
        return self.value


__all__ = [
    "CUSTODY_DISPOSED",
    "CUSTODY_EVENT_TYPES",
    "CUSTODY_LEGAL_HOLD_APPLIED",
    "CUSTODY_LEGAL_HOLD_RELEASED",
    "INTEGRITY_ALGORITHMS",
    "PUBLIC_SOURCE_AUTHORITY",
    "ArtifactType",
    "ConfidenceScore",
    "CustodyEventType",
    "EvidenceCategory",
    "IntegrityHash",
    "LegalAuthorityRef",
]
