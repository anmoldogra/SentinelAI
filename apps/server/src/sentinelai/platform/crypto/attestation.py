"""Independent attestation — verifying a ledger against WORM, not against itself.

ADR-0003 §3's argument for anchoring is that "the commitment lives outside the database the
attacker controls". Wave 1.4 built the verification engine that checks anchors, and it reads them
from :class:`~sentinelai.platform.auth.models.LedgerAnchor` — ``platform.ledger_anchors``, **a table
inside the database being audited**. Every layer of that verification is sound and the anchor layer
still catches an ordinary truncation, because deleting ledger rows while leaving the anchor rows
behind produces exactly the mismatch it looks for.

What it cannot catch is the attacker, or the restore, that removes both.

``deployment-architecture.md`` Part 14 states the consequence as a requirement: "after any restore
of a database containing ``platform.audit_log`` or ``ingestion.evidence_custody_events``,
re-verification against the external anchors is mandatory before the system is returned to service.
**The anchors live in the WORM anchor bucket, not in the database, so they survive the restore** and
will report exactly which committed entries are now missing." A point-in-time restore rolls back the
ledger *and* ``ledger_anchors`` together, leaving a database that is internally flawless — every
hash recomputes, every signature verifies, every anchor it still remembers reconciles perfectly. It
is a real, complete, self-consistent ledger. It is just an older one, and nothing inside it knows.

This module is what reads the bucket. It inverts the trust direction: **anchors are loaded from
WORM and the database's anchor table becomes a cross-check**, so the commitments a verdict rests on
are the ones the database cannot edit.

**Two questions, deliberately separate.**

1. *Does the ledger still contain what the published anchors committed to?* Answered by handing
   these WORM-sourced anchors to the existing :class:`~sentinelai.platform.crypto.verification.
   LedgerVerifier` via :meth:`AnchorDocument.to_anchor_view`. One implementation of the Merkle and
   range logic, exercised from both the online endpoint and here — a second copy would eventually
   disagree with the first about what counts as intact.
2. *Do the bucket and the database agree about which anchors exist?* Answered here, by
   :func:`reconcile_anchors`.

**The distinction that matters most in (2).** An anchor present in WORM and absent from the database
is *not*, on its own, evidence of tampering.
:meth:`~sentinelai.platform.crypto.anchoring.LedgerAnchorService.publish` writes the object before
the caller records the row, deliberately, so a crash between the two leaves an orphan object — and
that module's own docstring calls it "recoverable: the object is self-describing, so a
reconciliation pass can find it". This is that pass. So a missing row is reported as ``partial``:
bookkeeping to repair, nothing proven lost.

It becomes ``failed`` when question (1) also fails for the same anchor — when the range that anchor
committed to is no longer in the chain. That combination is the restore signature, and it is the
reason the two questions are asked of one report rather than by two tools: an orphan object whose
range is intact is an interrupted job, an orphan object whose range is gone is missing evidence, and
only something holding both answers can tell them apart.

**Read-only by construction.** Nothing here writes to the database, the bucket, or a ledger. It
issues ``SELECT``s (through its caller), ``GET``/``LIST`` against object storage, and KMS *verify*
calls, which use no private key. An auditor holding read-only database credentials and read access
to the anchor bucket can run all of it.
"""

from __future__ import annotations

import base64
import json
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final
from uuid import UUID

from sentinelai.platform.crypto.anchoring import ANCHOR_DOCUMENT_VERSION
from sentinelai.platform.crypto.ledger import LedgerSigner
from sentinelai.platform.crypto.verification import AnchorView, VerificationState
from sentinelai.platform.storage.exceptions import ObjectNotFound
from sentinelai.platform.storage.port import ObjectStorage


def anchor_prefix(ledger: str) -> str:
    """The object-storage prefix under which ``ledger``'s anchors are written.

    Must stay in step with :func:`~sentinelai.platform.crypto.anchoring.anchor_object_key`: a prefix
    that no longer matches the key scheme would list nothing, which reads as "this ledger has no
    anchors" and passes. ``test_attestation.py`` pins the two together rather than trusting this
    comment, because a silently-empty listing is the one failure this module cannot survive.
    """
    return f"anchors/{ledger}/"


class AttestationFinding(StrEnum):
    """Why the bucket and the database do not agree, or why an anchor object is unusable.

    Disjoint from :class:`~sentinelai.platform.crypto.verification.Finding`, which describes the
    ledger. These describe the *archive* and its relationship to the database — a different subject
    with different operator responses, and merging the two vocabularies would produce findings whose
    meaning depended on which layer emitted them.
    """

    # --- failures ---------------------------------------------------------------------------
    # A row claims an anchor that the bucket does not hold. Under COMPLIANCE-mode Object Lock the
    # object should have been undeletable, so this means either the lock was never real (a replica
    # that dropped retention - deployment-architecture.md Part 14 warns about exactly this) or
    # something with bucket administration removed it. Either way the database is asserting a
    # commitment that no longer exists anywhere.
    ANCHOR_OBJECT_MISSING = "anchor_object_missing"
    # The bucket's copy and the database's row disagree about what was committed. The bucket's copy
    # is the immutable one, so this reports an edited database row.
    ANCHOR_DOCUMENT_MISMATCH = "anchor_document_mismatch"
    # The document's own signature does not verify against the evidence key.
    ANCHOR_DOCUMENT_SIGNATURE_INVALID = "anchor_document_signature_invalid"
    # An object under the anchor prefix that is not a parseable anchor document. The writer only
    # ever emits canonical JSON of a known shape, so this is not a benign state.
    ANCHOR_DOCUMENT_MALFORMED = "anchor_document_malformed"

    # --- partials: something to reconcile, nothing proven lost -------------------------------
    # WORM holds an anchor the database has no row for. Benign after an interrupted publish;
    # damning when the same anchor's range is missing from the chain. See the module docstring.
    ANCHOR_MISSING_FROM_DATABASE = "anchor_missing_from_database"
    # Written by a later version of the writer. Refusing to interpret it is the only safe answer -
    # guessing a field layout would produce a mismatch and report a valid anchor as forged.
    ANCHOR_DOCUMENT_UNKNOWN_VERSION = "anchor_document_unknown_version"


_PARTIAL_FINDINGS: Final[frozenset[AttestationFinding]] = frozenset(
    {
        AttestationFinding.ANCHOR_MISSING_FROM_DATABASE,
        AttestationFinding.ANCHOR_DOCUMENT_UNKNOWN_VERSION,
    }
)


def _state_for(findings: Sequence[AttestationFinding]) -> VerificationState:
    if not findings:
        return VerificationState.VERIFIED
    if any(f not in _PARTIAL_FINDINGS for f in findings):
        return VerificationState.FAILED
    return VerificationState.PARTIAL


class AnchorDocumentError(ValueError):
    """An object under the anchor prefix is not a usable anchor document."""


class AnchorDocumentUnknownVersion(AnchorDocumentError):
    """The document declares a version this build does not know how to read.

    A subclass so a caller that does not care about the distinction still catches it, and a caller
    that does — the reader, which turns it into a ``partial`` rather than a ``failed`` — can.
    """


@dataclass(frozen=True, slots=True)
class AnchorDocument:
    """One anchor as the WORM bucket holds it — the inverse of
    :func:`~sentinelai.platform.crypto.anchoring.anchor_document`.

    ``object_key`` is carried because a finding has to name the object an auditor should go and look
    at, and because two documents claiming one ``anchor_id`` is itself worth being able to report.
    """

    anchor_id: UUID
    ledger: str
    merkle_root: str
    merkle_hash_algo: str
    first_entry_hash: str
    last_entry_hash: str
    entry_count: int
    created_at: str
    signature_envelope: bytes
    sig_alg: str
    key_id: str
    object_key: str
    tsa_token: bytes | None = None

    def to_anchor_view(self) -> AnchorView:
        """Adapt to what :class:`~sentinelai.platform.crypto.verification.LedgerVerifier` consumes.

        This is the whole point of the module: the verifier's three-layer logic is reused unchanged,
        but the anchors fed into it came from the bucket instead of from the table under audit.
        """
        return AnchorView(
            anchor_id=self.anchor_id,
            ledger=self.ledger,
            merkle_root=self.merkle_root,
            first_entry_hash=self.first_entry_hash,
            last_entry_hash=self.last_entry_hash,
            entry_count=self.entry_count,
            signature_envelope=self.signature_envelope,
            worm_object_ref=self.object_key,
            tsa_token=self.tsa_token,
        )


def _require_str(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise AnchorDocumentError(f"{key!r} missing or not a non-empty string")
    return value


def parse_anchor_document(raw: bytes, *, object_key: str) -> AnchorDocument:
    """Parse one WORM anchor object. Raises :class:`AnchorDocumentError` on anything unexpected.

    Strict on every field, because this is the one input to attestation that arrives from outside
    the database and a lenient parser would let a partially-corrupt archive read as a valid
    commitment. A document whose version this build does not know raises too — the caller turns that
    into :attr:`AttestationFinding.ANCHOR_DOCUMENT_UNKNOWN_VERSION`, which is a ``partial``, while
    every other parse failure is a ``failed``.
    """
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AnchorDocumentError(f"not JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise AnchorDocumentError("anchor document is not a JSON object")

    version = payload.get("v")
    if version != ANCHOR_DOCUMENT_VERSION:
        raise AnchorDocumentUnknownVersion(f"unsupported anchor document version {version!r}")

    try:
        anchor_id = UUID(_require_str(payload, "anchor_id"))
    except ValueError as exc:
        raise AnchorDocumentError(f"anchor_id is not a UUID: {exc}") from exc

    count = payload.get("entry_count")
    if not isinstance(count, int) or isinstance(count, bool) or count <= 0:
        raise AnchorDocumentError(f"entry_count is not a positive integer: {count!r}")

    try:
        signature = base64.b64decode(_require_str(payload, "signature"), validate=True)
    except (ValueError, TypeError) as exc:
        raise AnchorDocumentError(f"signature is not valid base64: {exc}") from exc

    token: bytes | None = None
    if (encoded := payload.get("tsa_token")) is not None:
        if not isinstance(encoded, str):
            raise AnchorDocumentError("tsa_token is present but not a string")
        try:
            token = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError) as exc:
            raise AnchorDocumentError(f"tsa_token is not valid base64: {exc}") from exc

    return AnchorDocument(
        anchor_id=anchor_id,
        ledger=_require_str(payload, "ledger"),
        merkle_root=_require_str(payload, "merkle_root"),
        merkle_hash_algo=_require_str(payload, "merkle_hash_algo"),
        first_entry_hash=_require_str(payload, "first_entry_hash"),
        last_entry_hash=_require_str(payload, "last_entry_hash"),
        entry_count=count,
        created_at=_require_str(payload, "created_at"),
        signature_envelope=signature,
        sig_alg=_require_str(payload, "sig_alg"),
        key_id=_require_str(payload, "key_id"),
        object_key=object_key,
        tsa_token=token,
    )


@dataclass(frozen=True, slots=True)
class MalformedAnchorObject:
    """An object under the anchor prefix that could not be parsed, and why."""

    object_key: str
    reason: str
    unknown_version: bool = False


@dataclass(frozen=True, slots=True)
class AnchorAttestation:
    """The archive-side verdict for one anchor.

    ``anchor_id`` is ``None`` for a malformed object, which has no readable identity — the
    ``object_key`` is then the only handle an auditor has, which is why it is never optional.
    """

    object_key: str
    state: VerificationState
    anchor_id: UUID | None = None
    in_worm: bool = True
    in_database: bool = True
    findings: tuple[AttestationFinding, ...] = ()
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class AttestationReport:
    """Whether the archive and the database agree about one ledger's anchors.

    Counts are explicit rather than derived, for the same reason
    :class:`~sentinelai.platform.crypto.verification.LedgerVerificationReport` carries its own: this
    is serialized to JSON for an operator and logged, and two consumers recomputing the same figure
    is two chances to disagree.
    """

    ledger: str
    state: VerificationState
    worm_anchors: int
    database_anchors: int
    reconciled: int
    missing_from_database: int
    missing_from_worm: int
    malformed_objects: int
    anchors: tuple[AnchorAttestation, ...] = ()

    @property
    def is_failed(self) -> bool:
        return self.state is VerificationState.FAILED

    @property
    def findings(self) -> tuple[AttestationFinding, ...]:
        return tuple(sorted({f for a in self.anchors for f in a.findings}))


def _disagreements(document: AnchorDocument, row: AnchorView) -> list[str]:
    """Which committed fields the bucket's copy and the database's row disagree about.

    Compared field by field rather than as a whole, so the report says *what* was changed. An
    operator seeing ``entry_count`` alone is looking at a narrowed range; one seeing ``merkle_root``
    alone is looking at a substituted commitment.

    ``signature_envelope`` is included because it is the thing that makes the row authentic: a
    database row carrying a different envelope from the published one is either a replay of another
    anchor's signature or a fabrication, and both must be visible even when every other field
    matches.
    """
    fields: tuple[tuple[str, object, object], ...] = (
        ("merkle_root", document.merkle_root, row.merkle_root),
        ("first_entry_hash", document.first_entry_hash, row.first_entry_hash),
        ("last_entry_hash", document.last_entry_hash, row.last_entry_hash),
        ("entry_count", document.entry_count, row.entry_count),
        ("ledger", document.ledger, row.ledger),
        ("signature_envelope", document.signature_envelope, row.signature_envelope),
    )
    return [name for name, published, stored in fields if published != stored]


def reconcile_anchors(
    *,
    documents: Sequence[AnchorDocument],
    malformed: Sequence[MalformedAnchorObject] = (),
    database_anchors: Sequence[AnchorView],
    signature_valid: Mapping[UUID, bool] | None = None,
) -> list[AnchorAttestation]:
    """Compare the bucket's anchors against the database's rows. Pure — no IO, no clock.

    ``signature_valid`` carries the outcome of verifying each document's own envelope, which is the
    one part of attestation that cannot be pure: it reaches a KMS. Passed in as a mapping rather
    than resolved here so this function stays testable against a tampered archive with no KMS at
    all, which is the same reason
    :mod:`~sentinelai.platform.crypto.verification` takes its signer by injection. A document absent
    from the mapping is treated as unverified rather than as valid — failing closed, because a
    missing verdict and a positive one must never be the same thing.
    """
    valid = signature_valid or {}
    by_id = {row.anchor_id: row for row in database_anchors}
    seen: set[UUID] = set()
    results: list[AnchorAttestation] = []

    for document in documents:
        findings: list[AttestationFinding] = []
        detail: str | None = None
        seen.add(document.anchor_id)

        if not valid.get(document.anchor_id, False):
            findings.append(AttestationFinding.ANCHOR_DOCUMENT_SIGNATURE_INVALID)

        row = by_id.get(document.anchor_id)
        if row is None:
            findings.append(AttestationFinding.ANCHOR_MISSING_FROM_DATABASE)
        else:
            changed = _disagreements(document, row)
            if changed:
                findings.append(AttestationFinding.ANCHOR_DOCUMENT_MISMATCH)
                detail = "database row disagrees with the published anchor on: " + ", ".join(
                    changed
                )

        results.append(
            AnchorAttestation(
                object_key=document.object_key,
                state=_state_for(findings),
                anchor_id=document.anchor_id,
                in_worm=True,
                in_database=row is not None,
                findings=tuple(findings),
                detail=detail,
            )
        )

    # Rows with no object. Ordered after the documents so a report reads "what the archive holds"
    # before "what the database claims that the archive does not".
    for row in database_anchors:
        if row.anchor_id in seen:
            continue
        results.append(
            AnchorAttestation(
                object_key=row.worm_object_ref,
                state=_state_for([AttestationFinding.ANCHOR_OBJECT_MISSING]),
                anchor_id=row.anchor_id,
                in_worm=False,
                in_database=True,
                findings=(AttestationFinding.ANCHOR_OBJECT_MISSING,),
                detail=(
                    "the database records this anchor but the WORM bucket does not hold it; under "
                    "COMPLIANCE-mode Object Lock the object should have been undeletable"
                ),
            )
        )

    for bad in malformed:
        finding = (
            AttestationFinding.ANCHOR_DOCUMENT_UNKNOWN_VERSION
            if bad.unknown_version
            else AttestationFinding.ANCHOR_DOCUMENT_MALFORMED
        )
        results.append(
            AnchorAttestation(
                object_key=bad.object_key,
                state=_state_for([finding]),
                anchor_id=None,
                in_worm=True,
                in_database=False,
                findings=(finding,),
                detail=bad.reason,
            )
        )
    return results


def build_attestation_report(
    *, ledger: str, attestations: Sequence[AnchorAttestation], database_anchors: int
) -> AttestationReport:
    """Roll per-anchor verdicts into one report."""
    return AttestationReport(
        ledger=ledger,
        state=(
            VerificationState.FAILED
            if any(a.state is VerificationState.FAILED for a in attestations)
            else VerificationState.PARTIAL
            if any(a.state is VerificationState.PARTIAL for a in attestations)
            else VerificationState.VERIFIED
        ),
        worm_anchors=sum(1 for a in attestations if a.in_worm and a.anchor_id is not None),
        database_anchors=database_anchors,
        reconciled=sum(1 for a in attestations if a.in_worm and a.in_database),
        missing_from_database=sum(
            1 for a in attestations if AttestationFinding.ANCHOR_MISSING_FROM_DATABASE in a.findings
        ),
        missing_from_worm=sum(
            1 for a in attestations if AttestationFinding.ANCHOR_OBJECT_MISSING in a.findings
        ),
        malformed_objects=sum(1 for a in attestations if a.anchor_id is None),
        anchors=tuple(attestations),
    )


class WormAnchorReader:
    """Reads a ledger's anchors out of the WORM bucket. The only IO in this module.

    Separate from the reconciliation so the comparison logic can be unit-tested against a tampered
    archive with no bucket and no KMS, and so an auditor's credentials need to grant nothing beyond
    ``GET`` and ``LIST`` on one prefix.
    """

    def __init__(self, storage: ObjectStorage, *, bucket: str) -> None:
        self._storage = storage
        self._bucket = bucket

    async def read(self, ledger: str) -> tuple[list[AnchorDocument], list[MalformedAnchorObject]]:
        """Every anchor object under ``ledger``'s prefix, parsed, plus those that would not parse.

        A single unparseable object never aborts the read. Returning what could be read alongside
        what could not is the difference between an attestation that reports one corrupt object and
        one that can produce no verdict at all — and a verifier an attacker can silence by
        corrupting a single object is not much of a verifier.
        """
        documents: list[AnchorDocument] = []
        malformed: list[MalformedAnchorObject] = []
        async for key in self._storage.list_prefix(self._bucket, anchor_prefix(ledger)):
            try:
                raw = await self._fetch(key)
            except ObjectNotFound:
                # Listed and then unreadable: a deletion racing this listing. Reported rather than
                # skipped, because a vanishing anchor is the event this whole module exists for.
                malformed.append(
                    MalformedAnchorObject(
                        object_key=key, reason="listed in the bucket but could not be read"
                    )
                )
                continue
            try:
                documents.append(parse_anchor_document(raw, object_key=key))
            except AnchorDocumentUnknownVersion as exc:
                malformed.append(
                    MalformedAnchorObject(object_key=key, reason=str(exc), unknown_version=True)
                )
            except AnchorDocumentError as exc:
                malformed.append(MalformedAnchorObject(object_key=key, reason=str(exc)))
        return documents, malformed

    async def _fetch(self, key: str) -> bytes:
        chunks: list[bytes] = []
        stream: AsyncIterator[bytes] = self._storage.get_stream(self._bucket, key)
        async for chunk in stream:
            chunks.append(chunk)
        return b"".join(chunks)


async def verify_document_signatures(
    signer: LedgerSigner, documents: Sequence[AnchorDocument]
) -> dict[UUID, bool]:
    """Verify each document's own signature envelope. Verify-only: touches no private key.

    Domain-separated as ``anchor:<ledger>`` over ``(entry_count, first_entry_hash, merkle_root)`` —
    the same four fields
    :meth:`~sentinelai.platform.crypto.anchoring.LedgerAnchorService.publish` signed, so a
    signature made for one anchor cannot be presented as another's, and an entry signature can never
    be replayed as an anchor's.
    """
    return {
        document.anchor_id: await signer.verify(
            ledger=f"anchor:{document.ledger}",
            sequence=document.entry_count,
            prev_hash=document.first_entry_hash,
            entry_hash=document.merkle_root,
            envelope=document.signature_envelope,
        )
        for document in documents
    }


__all__ = [
    "AnchorAttestation",
    "AnchorDocument",
    "AnchorDocumentError",
    "AnchorDocumentUnknownVersion",
    "AttestationFinding",
    "AttestationReport",
    "MalformedAnchorObject",
    "WormAnchorReader",
    "anchor_prefix",
    "build_attestation_report",
    "parse_anchor_document",
    "reconcile_anchors",
    "verify_document_signatures",
]
