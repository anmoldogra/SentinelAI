"""External anchoring of evidentiary ledgers — ADR-0003 §3.

The gap this closes, stated exactly: **signatures prove no entry was changed; anchors prove none
was removed.** An insider who deletes the tail of a chain, or restores yesterday's backup, leaves a
shorter ledger in which every remaining entry verifies perfectly — the evidence of what is missing
is precisely what was removed. No amount of checking inside the database can detect that, because
the database is what the attacker controls.

An anchor breaks the circularity by putting the commitment somewhere else: a Merkle root over a
batch of entries, signed under the evidence key, written to WORM object storage under a
COMPLIANCE-mode retention lock. Verification then asks a question the database cannot lie about —
*does the ledger still contain the entries this published root committed to?* — and a truncated
ledger fails it.

**What this does not yet provide: trusted time.** ADR-0003 §3 also calls for an RFC-3161
timestamp token, and `tsa_token_ref` is reserved for it but not populated. The distinction matters
and should not be glossed: WORM makes an anchor *undeletable*, which is what defeats truncation.
A TSA makes it *undatable-forward*, which defeats an attacker who controls both the application
and the clock and wants to publish a fresh anchor over a doctored history and claim it is old.
Anchors already written to WORM cannot be replaced, so truncation detection is real today;
backdating resistance is not.

Anchoring is a background activity, not part of a write transaction. A ledger append must never
wait on object storage — that would put a second network dependency inside the path ADR-0003 §1
already made KMS-dependent.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final
from uuid import UUID, uuid4

from sentinelai.platform.crypto.canonical import canonicalize
from sentinelai.platform.crypto.kms import KeyManagementService
from sentinelai.platform.crypto.ledger import LedgerSignature, LedgerSigner
from sentinelai.platform.crypto.merkle import MERKLE_HASH_ALGO, build_tree
from sentinelai.platform.crypto.tsa import (
    TimestampAuthority,
    TsaError,
    VerifiedTimestamp,
)
from sentinelai.platform.logging import log
from sentinelai.platform.storage.port import ObjectStorage

# Envelope version for the published anchor document. Independent of the ledger signature
# envelope's version: this is the container written to WORM, that is the signature inside it.
ANCHOR_DOCUMENT_VERSION: Final = 1

# Retention on the WORM object. Ten years is the floor for evidentiary retention in the profiles
# `deployment-architecture.md` targets; an anchor must outlive the evidence it commits to, because
# an anchor that expires first silently stops proving anything.
DEFAULT_RETENTION_YEARS: Final = 10


class AnchoringError(RuntimeError):
    """An anchor could not be built or published. Never swallowed — an unpublished anchor is not
    an anchor, and recording one in the database as if it were published would be worse than
    having none at all."""


@dataclass(frozen=True, slots=True)
class AnchorBatch:
    """The entries one anchor commits to, in ledger order."""

    ledger: str
    entry_hashes: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.entry_hashes:
            raise AnchoringError("refusing to anchor an empty batch")


@dataclass(frozen=True, slots=True)
class PublishedAnchor:
    """A published anchor, ready to be recorded in ``platform.ledger_anchors``."""

    anchor_id: UUID
    ledger: str
    merkle_root: str
    merkle_hash_algo: str
    first_entry_hash: str
    last_entry_hash: str
    entry_count: int
    created_at: datetime
    signature: LedgerSignature
    worm_object_ref: str
    # RFC 3161 token over `merkle_root` (Wave 1.3c). ``None`` when timestamping is disabled, which
    # is the air-gapped default and a legitimate state: the anchor still proves non-truncation via
    # WORM, it just carries no third-party proof of *when* it was made.
    tsa_token: bytes | None = None
    tsa_gen_time: datetime | None = None
    tsa_serial_number: int | None = None
    tsa_signer: str | None = None

    @property
    def is_timestamped(self) -> bool:
        return self.tsa_token is not None


def anchor_object_key(ledger: str, anchor_id: UUID, created_at: datetime) -> str:
    """Deterministic WORM key: sortable by time, unique by anchor id.

    The date prefix makes a bucket listing chronologically browsable, which is what an auditor
    reconstructing a timeline actually does with it.
    """
    return f"anchors/{ledger}/{created_at:%Y/%m/%d}/{anchor_id}.json"


def anchor_document(anchor: PublishedAnchor) -> bytes:
    """The bytes written to WORM — canonical JSON, self-describing.

    Deliberately **standalone**: it carries the root, the covered range, and the full signature
    envelope, so an auditor holding only this object and the public key can verify it without the
    database. That is the whole point — the database is the thing being checked.
    """
    return canonicalize(
        {
            "v": ANCHOR_DOCUMENT_VERSION,
            "anchor_id": str(anchor.anchor_id),
            "ledger": anchor.ledger,
            "merkle_root": anchor.merkle_root,
            "merkle_hash_algo": anchor.merkle_hash_algo,
            "first_entry_hash": anchor.first_entry_hash,
            "last_entry_hash": anchor.last_entry_hash,
            "entry_count": anchor.entry_count,
            "created_at": anchor.created_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            "signature": base64.b64encode(anchor.signature.envelope).decode(),
            "sig_alg": anchor.signature.sig_alg,
            "key_id": anchor.signature.key_id,
            # The RFC 3161 token travels INSIDE the WORM document, not only in the database row.
            # The whole point of the document standing alone is that an auditor holding the object
            # and the public keys can verify it without the database - and "when was this
            # committed" is exactly the question they most need answered without trusting us.
            # Omitted entirely rather than written as null when absent, so the document's shape
            # says plainly whether a timestamp was ever obtained.
            **(
                {
                    "tsa_token": base64.b64encode(anchor.tsa_token).decode(),
                    "tsa_gen_time": (
                        anchor.tsa_gen_time.astimezone(UTC).isoformat().replace("+00:00", "Z")
                        if anchor.tsa_gen_time
                        else None
                    ),
                    "tsa_serial_number": anchor.tsa_serial_number,
                }
                if anchor.tsa_token is not None
                else {}
            ),
        }
    )


class LedgerAnchorService:
    """Builds, signs, and publishes anchors. Does not touch the database.

    Persistence is the caller's job (``platform.auth.repository``), so this stays testable against
    a real KMS and a real object store without one, and so the transaction boundary stays with the
    entrypoint per ADR-0005.
    """

    def __init__(
        self,
        kms: KeyManagementService,
        storage: ObjectStorage,
        *,
        bucket: str,
        retention_years: int = DEFAULT_RETENTION_YEARS,
        timestamp_authority: TimestampAuthority | None = None,
    ) -> None:
        self._signer = LedgerSigner(kms)
        self._storage = storage
        self._bucket = bucket
        self._retention_years = retention_years
        # ``None`` is the air-gapped configuration and is expressed by absence rather than by a
        # flag,
        # so there is no `if enabled` branch inside the publish path to get wrong.
        self._tsa = timestamp_authority

    async def publish(self, batch: AnchorBatch, *, now: datetime | None = None) -> PublishedAnchor:
        """Build the root, sign it, and write it to WORM. Returns what to record.

        Order matters and is not arbitrary: the object is written **before** the caller records
        the anchor row. A row pointing at an object that was never written would be a database
        claiming an anchor exists when it does not — the precise lie this subsystem exists to make
        impossible. The reverse failure (an object with no row) is recoverable: the object is
        self-describing, so a reconciliation pass can find it.
        """
        created_at = (now or datetime.now(UTC)).astimezone(UTC)
        tree = build_tree(list(batch.entry_hashes))
        anchor_id = uuid4()
        first, last = batch.entry_hashes[0], batch.entry_hashes[-1]

        # Signed through the same primitive ledger entries use, so one verification path covers
        # both. The four signed fields carry everything an anchor must bind:
        #   ledger      -> "anchor:<chain>", a domain separator distinct from the chain's own
        #                  entry signatures, so an anchor signature cannot be replayed as an entry
        #                  signature or vice versa;
        #   sequence    -> the entry count, so a silently narrowed range is detectable;
        #   prev_hash   -> the first entry hash, pinning where the range starts;
        #   entry_hash  -> the Merkle root, committing to every entry and their order.
        # The root alone would be insufficient: it commits to a set of leaves but says nothing
        # about which ledger or which range, so it could be replayed against a different chain.
        signature = await self._signer.sign(
            ledger=f"anchor:{batch.ledger}",
            sequence=len(batch.entry_hashes),
            prev_hash=first,
            entry_hash=tree.root,
        )
        # RFC 3161 (Wave 1.3c). Timestamped over the Merkle root's own bytes - the same value the
        # anchor signature covers - so the token attests to exactly what the anchor commits to, and
        # an auditor needs no extra context to know what was timestamped.
        #
        # **Fails OPEN, and only here.** Every other failure in this method aborts the anchor: an
        # unsigned anchor or an unpublished WORM object would be a lie. An untimestamped anchor is
        # not a lie, it is a weaker true statement - it still proves non-truncation, which is the
        # attack WORM exists to stop. Aborting the cut because a third party was unreachable would
        # mean an outage at someone else's TSA stops this platform committing to its own evidence,
        # trading the guarantee we control for the one we do not. The absence is recorded, never
        # inferred: `tsa_token` stays None and the verifier reports the anchor as untimestamped.
        tsa_token: bytes | None = None
        verified: VerifiedTimestamp | None = None
        if self._tsa is not None:
            try:
                tsa_token, verified = await self._tsa.timestamp(tree.root.encode("ascii"))
            except TsaError as exc:
                log.warning(
                    "anchor_timestamp_unavailable",
                    ledger=batch.ledger,
                    error=type(exc).__name__,
                    detail=str(exc),
                )

        anchor = PublishedAnchor(
            anchor_id=anchor_id,
            ledger=batch.ledger,
            merkle_root=tree.root,
            merkle_hash_algo=MERKLE_HASH_ALGO,
            first_entry_hash=first,
            last_entry_hash=last,
            entry_count=len(batch.entry_hashes),
            created_at=created_at,
            signature=signature,
            worm_object_ref=anchor_object_key(batch.ledger, anchor_id, created_at),
            tsa_token=tsa_token,
            tsa_gen_time=verified.gen_time if verified else None,
            tsa_serial_number=verified.serial_number if verified else None,
            tsa_signer=verified.signer_subject if verified else None,
        )
        try:
            await self._storage.put_immutable(
                self._bucket,
                anchor.worm_object_ref,
                anchor_document(anchor),
                retain_until=created_at.replace(year=created_at.year + self._retention_years),
                content_type="application/json",
            )
        except Exception as exc:
            raise AnchoringError(f"anchor could not be published to WORM storage: {exc}") from exc
        return anchor

    async def verify_signature(self, anchor: PublishedAnchor) -> bool:
        """Whether the anchor's own signature is valid — that it is ours and unaltered."""
        return await self._signer.verify(
            ledger=f"anchor:{anchor.ledger}",
            sequence=anchor.entry_count,
            prev_hash=anchor.first_entry_hash,
            entry_hash=anchor.merkle_root,
            envelope=anchor.signature.envelope,
        )


def verify_batch_against_anchor(
    entry_hashes: list[str] | tuple[str, ...],
    *,
    merkle_root: str,
    entry_count: int,
) -> bool:
    """Does this sequence of ledger entries still match what the anchor committed to?

    This is the truncation and rollback check. Recomputing the root over what the ledger holds
    *now* and comparing it with what was published *then* answers it: a removed entry, a reordered
    pair, or a rewritten one all produce a different root.

    The count is compared first and separately. It cannot change the verdict — a different count
    yields a different root anyway — but it makes the *reason* legible to the caller building a
    court-facing report, where "three entries are missing" and "one entry was altered" are
    different findings even though both are a root mismatch.
    """
    if len(entry_hashes) != entry_count:
        return False
    if not entry_hashes:
        return False
    return build_tree(list(entry_hashes)).root == merkle_root


__all__ = [
    "ANCHOR_DOCUMENT_VERSION",
    "DEFAULT_RETENTION_YEARS",
    "AnchorBatch",
    "AnchoringError",
    "LedgerAnchorService",
    "PublishedAnchor",
    "anchor_document",
    "anchor_object_key",
    "verify_batch_against_anchor",
]
