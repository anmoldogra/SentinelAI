"""Evidentiary ledger verification — ADR-0003 §6, modernization Wave 1.4.

Waves 1.1-1.3 made the ledgers *tamper-evident*: a complete preimage under RFC 8785 JCS
(Wave 1.2), an Ed25519 signature under a KMS key the database role cannot read (Wave 1.2), and a
Merkle root published to WORM storage (Wave 1.3). None of that is *tamper-detecting*. Evidence was
only ever detectable in principle, because nothing read it back and checked. This module is what
turns the construction into a verdict.

**Three independent layers, and the independence is the point.**

1. **Link continuity** — does each entry name its predecessor's ``entry_hash``, and (where the
   ledger has one) does its sequence advance by exactly one? This catches a removed or reordered
   *interior* entry, and it needs no keys and no network.
2. **Entry authenticity** — recomputing ``entry_hash`` from the row's own persisted fields catches
   an edited column; verifying the signature envelope catches an attacker who recomputed the whole
   chain. A verifier that checked the hash and skipped the signature would report a rewritten
   ledger as intact, because nothing about a hash requires a secret.
3. **Anchor inclusion** — recomputing the Merkle root over what the ledger holds *now* and
   comparing it against what was published *then* catches a removed **tail**, which layers 1 and 2
   structurally cannot: delete the last N entries and every survivor still links, still hashes,
   still verifies. The evidence of what is missing is exactly what was removed.

Skipping any one layer leaves a whole attack class unreported, so :class:`LedgerVerifier` always
runs all three and merges their findings per entry.

**Why this module is pure.** It touches no database, no object store, and knows nothing about
evidence, cases, or any domain concept — callers hand it :class:`LedgerEntryView` records they
extracted, and it hands back findings. Three reasons: ``platform`` may not import a module
(import-linter ``platform is domain-agnostic``), so the custody ledger's field set has to arrive
from ``modules.ingestion`` rather than be known here; a verifier that issued its own queries could
not be tested against a tampered chain without a database; and the court-facing report and the
scheduled re-verification job must run *identical* logic, which is only guaranteed if there is one
implementation and it has no I/O to differ in.

The one exception is signature verification, which is necessarily async and necessarily reaches a
KMS — :class:`LedgerSigner` is injected for exactly that, and nothing else.

**Three states, not two.** ``verified`` / ``partial`` / ``failed``. The middle state is not
hedging: rows written before Wave 1.2 carry ``preimage_version = NULL`` and ``signature = NULL``,
and they can never be signed retroactively with any honesty, because the bytes that should have
been signed are gone. Collapsing ``partial`` into ``failed`` would slander legitimate history;
collapsing it into ``verified`` would whitewash an unsigned row. A custody report that cannot tell
"proven", "not provable", and "forged" apart is not usable in a courtroom.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final
from uuid import UUID

from sentinelai.platform.crypto.anchoring import verify_batch_against_anchor
from sentinelai.platform.crypto.canonical import CanonicalizationError
from sentinelai.platform.crypto.ledger import (
    LEDGER_HASH_ALGO,
    LEDGER_PREIMAGE_VERSION,
    LedgerPreimageError,
    LedgerSigner,
    compute_entry_hash,
)

# The all-zero sentinel every chain starts from (CEM §4). Stored literally on the genesis entry and
# hashed into it, so it is a real value to compare against rather than an absence.
GENESIS_HASH: Final = "0" * 64


class VerificationState(StrEnum):
    """The verdict for one entry, one anchor, or a whole chain.

    Ordered by severity for aggregation: a chain is ``failed`` if anything in it failed,
    ``partial`` if anything is unprovable and nothing failed, and ``verified`` only when every
    component was positively proven.
    """

    VERIFIED = "verified"
    PARTIAL = "partial"
    FAILED = "failed"


class Finding(StrEnum):
    """Why an entry or anchor is not plainly ``verified``.

    Granular on purpose. "The chain is broken" is not a usable finding for someone deciding
    whether a case is still prosecutable — "entry 7's signature does not verify" and "entries 8-12
    are missing from an anchored range" lead to completely different conclusions, even though both
    reduce to a mismatch somewhere.
    """

    # --- failures: something is positively wrong -------------------------------------------
    LINK_BROKEN = "link_broken"
    SEQUENCE_GAP = "sequence_gap"
    SEQUENCE_DUPLICATE = "sequence_duplicate"
    HASH_MISMATCH = "hash_mismatch"
    SIGNATURE_INVALID = "signature_invalid"
    GENESIS_MISSING = "genesis_missing"
    ANCHOR_SIGNATURE_INVALID = "anchor_signature_invalid"
    ANCHOR_ROOT_MISMATCH = "anchor_root_mismatch"
    ANCHOR_RANGE_MISSING = "anchor_range_missing"
    ANCHOR_ENTRY_COUNT_MISMATCH = "anchor_entry_count_mismatch"

    # --- partials: nothing is wrong, but nothing is proven either ---------------------------
    UNSIGNED_LEGACY_ROW = "unsigned_legacy_row"
    PREIMAGE_UNAVAILABLE = "preimage_unavailable"
    UNKNOWN_PREIMAGE_VERSION = "unknown_preimage_version"
    UNSUPPORTED_HASH_ALGO = "unsupported_hash_algo"


# Findings that mean "not independently verifiable" rather than "wrong". Kept as one frozen set so
# the classification lives in a single place — a new Finding added on the wrong side of this line
# would silently turn a forgery into a footnote, or an honest legacy row into an alarm.
_PARTIAL_FINDINGS: Final[frozenset[Finding]] = frozenset(
    {
        Finding.UNSIGNED_LEGACY_ROW,
        Finding.PREIMAGE_UNAVAILABLE,
        Finding.UNKNOWN_PREIMAGE_VERSION,
        Finding.UNSUPPORTED_HASH_ALGO,
    }
)


def _state_for(findings: Sequence[Finding]) -> VerificationState:
    """Severity merge for one component's findings."""
    if not findings:
        return VerificationState.VERIFIED
    if any(f not in _PARTIAL_FINDINGS for f in findings):
        return VerificationState.FAILED
    return VerificationState.PARTIAL


def _merge(states: Sequence[VerificationState]) -> VerificationState:
    """Severity merge across components — failed dominates partial dominates verified."""
    if VerificationState.FAILED in states:
        return VerificationState.FAILED
    if VerificationState.PARTIAL in states:
        return VerificationState.PARTIAL
    return VerificationState.VERIFIED


@dataclass(frozen=True, slots=True)
class LedgerEntryView:
    """One ledger row, reduced to what verification needs and nothing else.

    Built by whoever owns the table: ``platform.auth`` for ``platform.audit_log``,
    ``modules.ingestion`` for ``ingestion.evidence_custody_events``. That split is forced by the
    import DAG and is also correct on its own terms — the preimage field set *is* the table's
    schema, and the module that owns the schema is the only place that can be trusted to stay in
    step with it.

    ``preimage_fields`` is the exact mapping the writer passed to
    :func:`~sentinelai.platform.crypto.ledger.compute_entry_hash`, rebuilt from the persisted row.
    ``None`` means the row cannot be re-derived at all (a pre-Wave-1.2 partial preimage), which is
    a ``partial``, never a ``failed``.
    """

    sequence: int | None
    entry_hash: str
    prev_hash: str
    hash_algo: str | None
    preimage_version: int | None
    preimage_fields: Mapping[str, object] | None
    signature_envelope: bytes | None


@dataclass(frozen=True, slots=True)
class AnchorView:
    """One row of ``platform.ledger_anchors``, plus its signature envelope."""

    anchor_id: UUID
    ledger: str
    merkle_root: str
    first_entry_hash: str
    last_entry_hash: str
    entry_count: int
    signature_envelope: bytes | None
    worm_object_ref: str


@dataclass(frozen=True, slots=True)
class EntryFinding:
    """Per-entry verdict, carrying enough identity to be actionable in a report."""

    sequence: int | None
    entry_hash: str
    state: VerificationState
    findings: tuple[Finding, ...] = ()


@dataclass(frozen=True, slots=True)
class AnchorFinding:
    """Per-anchor verdict. ``covered_entries`` is what the ledger holds for the range *now*."""

    anchor_id: UUID
    state: VerificationState
    expected_entry_count: int
    covered_entries: int
    worm_object_ref: str
    findings: tuple[Finding, ...] = ()


@dataclass(frozen=True, slots=True)
class LedgerVerificationReport:
    """The court-facing result for one chain.

    Counts are carried explicitly rather than left for a caller to derive: this object is
    serialized into an API response and written to a log line, and two consumers recomputing
    "how many entries failed" from the findings list is two chances to disagree.
    """

    ledger: str
    state: VerificationState
    entry_count: int
    verified_entries: int
    partial_entries: int
    failed_entries: int
    entries: tuple[EntryFinding, ...] = ()
    anchors: tuple[AnchorFinding, ...] = ()
    unanchored_entries: int = 0
    findings: tuple[Finding, ...] = field(default=())

    @property
    def is_failed(self) -> bool:
        """Whether this report should raise an alarm. Read by the re-verification job."""
        return self.state is VerificationState.FAILED


class LedgerVerifier:
    """Runs all three verification layers over one chain. Holds no state between calls.

    ``signer`` is used for verification only — :meth:`LedgerSigner.verify` never touches a private
    key, so this is safe to run in a read-only context (the online endpoint) as well as in the
    worker.
    """

    def __init__(self, signer: LedgerSigner) -> None:
        self._signer = signer

    async def verify_chain(
        self,
        *,
        ledger: str,
        entries: Sequence[LedgerEntryView],
        anchors: Sequence[AnchorView] = (),
        chain_entry_hashes: Sequence[str] | None = None,
        expect_genesis: bool = True,
    ) -> LedgerVerificationReport:
        """Verify ``entries`` (in ledger order) against themselves and against ``anchors``.

        ``entries`` must be ordered as the ledger orders them — by ``sequence_number`` for custody,
        by the chain itself for audit. Verification cannot sort them: the order *is* part of what
        is being checked, so sorting here would repair the very tampering the caller wants
        detected.

        ``chain_entry_hashes`` is the **whole** chain's entry hashes in order, used only for the
        anchor layer. It exists because the two layers need different scopes and conflating them
        produces false alarms in one direction and missed truncation in the other. The audit ledger
        is global and unbounded, so a caller verifies a bounded window of entries in detail — but an
        anchor commits to a range that may sit entirely outside that window, and checking it against
        the window alone would report every out-of-window anchor as a missing range. Passing only
        the window would be worse than useless: the *reason* to check anchors is to catch a deleted
        tail, and a deleted tail is exactly what falls outside a window of surviving rows. Defaults
        to the hashes of ``entries``, which is correct whenever the caller passed a complete chain
        (as the custody endpoint does).

        ``expect_genesis`` is ``False`` for a chain that is legitimately a window rather than a
        whole history. With it ``True``, a first entry whose ``prev_hash`` is not the genesis
        sentinel is a missing head, not a fresh start.
        """
        per_entry = await self._verify_entries(
            ledger=ledger, entries=entries, expect_genesis=expect_genesis
        )
        hashes = (
            list(chain_entry_hashes)
            if chain_entry_hashes is not None
            else [e.entry_hash for e in entries]
        )
        anchor_findings = await self._verify_anchors(chain_entry_hashes=hashes, anchors=anchors)

        verified = sum(1 for e in per_entry if e.state is VerificationState.VERIFIED)
        partial = sum(1 for e in per_entry if e.state is VerificationState.PARTIAL)
        failed = sum(1 for e in per_entry if e.state is VerificationState.FAILED)

        anchored = self._anchored_entry_hashes(chain_entry_hashes=hashes, anchors=anchors)
        unanchored = sum(1 for h in hashes if h not in anchored)

        state = _merge(
            [e.state for e in per_entry] + [a.state for a in anchor_findings],
        )
        return LedgerVerificationReport(
            ledger=ledger,
            state=state,
            entry_count=len(entries),
            verified_entries=verified,
            partial_entries=partial,
            failed_entries=failed,
            entries=tuple(per_entry),
            anchors=tuple(anchor_findings),
            unanchored_entries=unanchored,
            findings=tuple(sorted({f for e in per_entry for f in e.findings})),
        )

    # -- layer 1 + 2 -------------------------------------------------------------------------

    async def _verify_entries(
        self, *, ledger: str, entries: Sequence[LedgerEntryView], expect_genesis: bool
    ) -> list[EntryFinding]:
        results: list[EntryFinding] = []
        expected_prev: str | None = GENESIS_HASH if expect_genesis else None
        previous_sequence: int | None = None

        for index, entry in enumerate(entries):
            findings: list[Finding] = []

            # --- layer 1: link continuity ---
            if expected_prev is not None and entry.prev_hash != expected_prev:
                # The first entry is a special case worth reporting differently: a wrong prev on
                # entry 0 means the head of the chain is gone, which reads very differently in a
                # report from a break in the middle.
                findings.append(
                    Finding.GENESIS_MISSING if index == 0 else Finding.LINK_BROKEN,
                )
            if entry.sequence is not None and previous_sequence is not None:
                if entry.sequence == previous_sequence:
                    findings.append(Finding.SEQUENCE_DUPLICATE)
                elif entry.sequence != previous_sequence + 1:
                    findings.append(Finding.SEQUENCE_GAP)

            # --- layer 2: entry authenticity ---
            findings.extend(self._check_entry_hash(entry))
            findings.extend(await self._check_signature(ledger=ledger, entry=entry))

            results.append(
                EntryFinding(
                    sequence=entry.sequence,
                    entry_hash=entry.entry_hash,
                    state=_state_for(findings),
                    findings=tuple(findings),
                )
            )
            expected_prev = entry.entry_hash
            previous_sequence = entry.sequence
        return results

    def _check_entry_hash(self, entry: LedgerEntryView) -> list[Finding]:
        """Recompute ``entry_hash`` from the row's own fields, or explain why that is impossible.

        The three "impossible" cases are all ``partial``, and the distinction between them matters
        to whoever reads the report:

        * ``preimage_version`` is ``NULL`` — a pre-Wave-1.2 row, hashed over an incomplete field
          set with a non-canonical encoder. Not re-derivable by this code at all.
        * ``preimage_version`` is some *other* number — written by a future version of the writer.
          Refusing is the only safe answer; guessing the field set would produce a mismatch and
          report a valid entry as forged.
        * ``hash_algo`` is not the one this build computes. Crypto agility means history stays
          verifiable under the algorithm it was *written* with, so a row written under SHA-384
          cannot be checked by a SHA-256 recompute. It is not wrong; it is out of this build's
          reach.
        """
        if entry.preimage_fields is None or entry.preimage_version is None:
            return [Finding.PREIMAGE_UNAVAILABLE]
        if entry.preimage_version != LEDGER_PREIMAGE_VERSION:
            return [Finding.UNKNOWN_PREIMAGE_VERSION]
        if entry.hash_algo is not None and entry.hash_algo != LEDGER_HASH_ALGO:
            return [Finding.UNSUPPORTED_HASH_ALGO]
        try:
            recomputed = compute_entry_hash(entry.preimage_fields)
        except (LedgerPreimageError, CanonicalizationError):
            # The stored fields cannot be turned into a preimage at all — either the field set is
            # malformed (`LedgerPreimageError`) or a value is not representable under RFC 8785
            # (`CanonicalizationError`). Neither is a legacy row and neither is tolerable: something
            # in this row is not the bytes it was supposedly hashed from.
            #
            # Caught rather than propagated so one unrepresentable row cannot deny a verdict on the
            # entire chain. A verifier that raised here would hand an attacker a denial-of-proof:
            # corrupt one field and no report can be produced for any entry.
            return [Finding.HASH_MISMATCH]
        return [] if recomputed == entry.entry_hash else [Finding.HASH_MISMATCH]

    async def _check_signature(self, *, ledger: str, entry: LedgerEntryView) -> list[Finding]:
        """Verify the envelope, distinguishing "never signed" from "does not verify".

        ``LedgerSigner.verify`` returns ``False`` for both, which is correct for its purpose but
        useless for a report: one is history that predates signing, the other is a forgery.
        """
        if entry.signature_envelope is None:
            return [Finding.UNSIGNED_LEGACY_ROW]
        valid = await self._signer.verify(
            ledger=ledger,
            sequence=entry.sequence,
            prev_hash=entry.prev_hash,
            entry_hash=entry.entry_hash,
            envelope=entry.signature_envelope,
        )
        return [] if valid else [Finding.SIGNATURE_INVALID]

    # -- layer 3 -----------------------------------------------------------------------------

    async def _verify_anchors(
        self, *, chain_entry_hashes: Sequence[str], anchors: Sequence[AnchorView]
    ) -> list[AnchorFinding]:
        hashes = list(chain_entry_hashes)
        results: list[AnchorFinding] = []

        for anchor in anchors:
            findings: list[Finding] = []
            covered: list[str] = []

            window = _slice_between(hashes, anchor.first_entry_hash, anchor.last_entry_hash)
            if window is None:
                # Either end of the committed range is no longer in the ledger. This is the
                # truncation signature: the anchor names entries that have ceased to exist.
                findings.append(Finding.ANCHOR_RANGE_MISSING)
            else:
                covered = window
                if len(covered) != anchor.entry_count:
                    findings.append(Finding.ANCHOR_ENTRY_COUNT_MISMATCH)
                if not verify_batch_against_anchor(
                    covered, merkle_root=anchor.merkle_root, entry_count=anchor.entry_count
                ):
                    findings.append(Finding.ANCHOR_ROOT_MISMATCH)

            # The anchor's own signature is checked regardless: an attacker who forged an anchor to
            # match a doctored ledger would produce a range that reconciles perfectly, and only the
            # signature exposes it. Domain-separated as `anchor:<ledger>` by the signing path, so an
            # entry signature can never be replayed here.
            if not await self._signer.verify(
                ledger=f"anchor:{anchor.ledger}",
                sequence=anchor.entry_count,
                prev_hash=anchor.first_entry_hash,
                entry_hash=anchor.merkle_root,
                envelope=anchor.signature_envelope,
            ):
                findings.append(Finding.ANCHOR_SIGNATURE_INVALID)

            results.append(
                AnchorFinding(
                    anchor_id=anchor.anchor_id,
                    state=_state_for(findings),
                    expected_entry_count=anchor.entry_count,
                    covered_entries=len(covered),
                    worm_object_ref=anchor.worm_object_ref,
                    findings=tuple(findings),
                )
            )
        return results

    @staticmethod
    def _anchored_entry_hashes(
        *, chain_entry_hashes: Sequence[str], anchors: Sequence[AnchorView]
    ) -> frozenset[str]:
        """Which entry hashes fall inside some anchor's committed range.

        Entries outside every range are **not** a finding. Anchoring is batched, so the newest
        entries are legitimately unanchored until the next batch is cut; calling that a failure
        would make a healthy ledger alarm continuously. It is reported as a count so an operator
        can notice a batch job that has silently stopped.
        """
        hashes = list(chain_entry_hashes)
        covered: set[str] = set()
        for anchor in anchors:
            window = _slice_between(hashes, anchor.first_entry_hash, anchor.last_entry_hash)
            if window is not None:
                covered.update(window)
        return frozenset(covered)


def _slice_between(hashes: Sequence[str], first: str, last: str) -> list[str] | None:
    """The contiguous run of ``hashes`` from ``first`` to ``last``, or ``None`` if not present.

    ``None`` when either end is absent, or when ``last`` precedes ``first`` — both mean the anchored
    range no longer exists in the ledger as a range, which is what an anchor verification needs to
    know. Returned as the *current* contents of that window, so the caller can recompute a root
    over what is actually there and compare it with what was committed.
    """
    try:
        start = hashes.index(first)
        end = hashes.index(last)
    except ValueError:
        return None
    if end < start:
        return None
    return list(hashes[start : end + 1])


__all__ = [
    "GENESIS_HASH",
    "AnchorFinding",
    "AnchorView",
    "EntryFinding",
    "Finding",
    "LedgerEntryView",
    "LedgerVerificationReport",
    "LedgerVerifier",
    "VerificationState",
]
