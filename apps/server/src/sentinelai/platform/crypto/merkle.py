"""Merkle tree over ledger entries — ADR-0003 §3.

Signing (ADR-0003 §1) makes *modification* detectable: an entry cannot be rewritten without
invalidating its signature. It says nothing about *removal*. An insider who deletes the last N
entries — or restores yesterday's backup — leaves a shorter chain in which every remaining entry
verifies perfectly, because the evidence of the missing entries is exactly what was removed.

Closing that needs a commitment published somewhere the insider cannot reach: a Merkle root over a
batch of entries, signed, and written to WORM storage. Later, "does the ledger still contain
everything the anchor committed to?" becomes answerable, and a truncated ledger fails it. This
module builds and verifies those roots.

**Second-preimage resistance.** Leaves are hashed with a ``0x00`` prefix and interior nodes with
``0x01``. Without that, a tree over N leaves and a tree over their interior digests can produce the
same root, letting an attacker present an internal node as if it were a leaf — the classic flaw in
naive Merkle constructions (and the reason RFC 6962 does the same thing). The prefixes are what
make "this entry is in this tree" a claim about a *leaf*.

**Odd nodes are promoted, not duplicated.** A level with an odd count carries its last node up
unchanged rather than hashing it against itself. Duplicating is the CVE-2012-2459 pattern, where
two different leaf sets yield one root.

Pure and dependency-free: no I/O, no clock, no key material. Anchoring policy (when to cut a
batch, where to write the root) belongs to the caller.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Final

# Domain-separation prefixes, per RFC 6962 §2.1.
_LEAF_PREFIX: Final = b"\x00"
_NODE_PREFIX: Final = b"\x01"

MERKLE_HASH_ALGO: Final = "SHA-256"

# The root of an empty tree. A batch is never anchored empty — `build_tree` refuses — but the
# constant is defined so a verifier meeting one can recognise it rather than guess.
EMPTY_ROOT: Final = hashlib.sha256(b"").hexdigest()


class MerkleError(ValueError):
    """A Merkle tree could not be built or a proof could not be checked."""


def leaf_hash(value: str) -> str:
    """Hash one ledger entry hash into a Merkle leaf.

    Takes the entry's hex ``entry_hash`` and commits to its bytes as text. The entry hash already
    covers the whole entry (ADR-0003 §2), so committing to it commits to the entry.
    """
    return hashlib.sha256(_LEAF_PREFIX + value.encode("utf-8")).hexdigest()


def _node_hash(left: str, right: str) -> str:
    return hashlib.sha256(_NODE_PREFIX + bytes.fromhex(left) + bytes.fromhex(right)).hexdigest()


@dataclass(frozen=True, slots=True)
class MerkleTree:
    """A built tree: its leaves, its levels, and its root.

    ``levels[0]`` is the leaf hashes; the last level is a single-element list holding the root.
    Keeping the levels lets an inclusion proof be produced without rebuilding.
    """

    leaves: tuple[str, ...]
    levels: tuple[tuple[str, ...], ...]

    @property
    def root(self) -> str:
        return self.levels[-1][0]

    @property
    def size(self) -> int:
        return len(self.leaves)


def build_tree(entry_hashes: list[str] | tuple[str, ...]) -> MerkleTree:
    """Build a Merkle tree over ledger entry hashes, in the order given.

    **Order is significant and is not sorted.** The ledger's order *is* the evidence — a custody
    chain reordered is a different history — so the tree commits to the sequence, not just the set.

    An empty batch raises rather than returning :data:`EMPTY_ROOT`: anchoring nothing would publish
    a commitment that proves nothing while looking like proof.
    """
    if not entry_hashes:
        raise MerkleError("cannot build a Merkle tree over an empty batch")

    leaves = tuple(leaf_hash(value) for value in entry_hashes)
    levels: list[tuple[str, ...]] = [leaves]
    current = leaves
    while len(current) > 1:
        nxt: list[str] = []
        for index in range(0, len(current) - 1, 2):
            nxt.append(_node_hash(current[index], current[index + 1]))
        if len(current) % 2:
            # Promoted, never doubled — see the module docstring on CVE-2012-2459.
            nxt.append(current[-1])
        current = tuple(nxt)
        levels.append(current)
    return MerkleTree(leaves=tuple(entry_hashes), levels=tuple(levels))


def merkle_root(entry_hashes: list[str] | tuple[str, ...]) -> str:
    """The root over ``entry_hashes``. Convenience for callers that need no proofs."""
    return build_tree(entry_hashes).root


@dataclass(frozen=True, slots=True)
class InclusionProof:
    """Evidence that one entry is in a tree, without needing the rest of it.

    ``path`` is the sibling at each level, with the side it sits on. This is what lets a court
    presentation prove one custody entry is covered by a published anchor while disclosing nothing
    about unrelated entries in the same batch — which matters when a batch spans cases.
    """

    index: int
    entry_hash: str
    path: tuple[tuple[str, str], ...]  # (sibling_hash, "left" | "right")


def inclusion_proof(tree: MerkleTree, index: int) -> InclusionProof:
    """Produce the audit path for the leaf at ``index``."""
    if not 0 <= index < tree.size:
        raise MerkleError(f"index {index} is outside a tree of {tree.size} leaves")

    path: list[tuple[str, str]] = []
    position = index
    for level in tree.levels[:-1]:
        if position % 2:
            path.append((level[position - 1], "left"))
        elif position + 1 < len(level):
            path.append((level[position + 1], "right"))
        # else: an odd node promoted unchanged — no sibling at this level, so nothing to record.
        position //= 2
    return InclusionProof(index=index, entry_hash=tree.leaves[index], path=tuple(path))


def verify_inclusion(proof: InclusionProof, root: str) -> bool:
    """Recompute the root from a proof. ``False``, never an exception, on a malformed path.

    A proof that does not check out is an invalid proof, not an error — it is presented as
    evidence and it fails, which is a finding rather than a fault.
    """
    try:
        computed = leaf_hash(proof.entry_hash)
        for sibling, side in proof.path:
            computed = (
                _node_hash(sibling, computed) if side == "left" else _node_hash(computed, sibling)
            )
    except (ValueError, TypeError):
        return False
    return computed == root


__all__ = [
    "EMPTY_ROOT",
    "MERKLE_HASH_ALGO",
    "InclusionProof",
    "MerkleError",
    "MerkleTree",
    "build_tree",
    "inclusion_proof",
    "leaf_hash",
    "merkle_root",
    "verify_inclusion",
]
