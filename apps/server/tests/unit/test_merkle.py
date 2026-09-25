"""Merkle tree over ledger entries — ADR-0003 §3.

The properties that matter are the ones a naive implementation gets wrong, so those are tested
first and explicitly: second-preimage resistance via leaf/node domain separation, and the
odd-node promotion that avoids CVE-2012-2459's two-leaf-sets-one-root collision. A tree that
computes *a* root is easy; a tree whose root means something is not.
"""

from __future__ import annotations

import hashlib

import pytest

from sentinelai.platform.crypto.merkle import (
    EMPTY_ROOT,
    InclusionProof,
    MerkleError,
    build_tree,
    inclusion_proof,
    leaf_hash,
    merkle_root,
    verify_inclusion,
)


def _hashes(count: int) -> list[str]:
    return [hashlib.sha256(str(i).encode()).hexdigest() for i in range(count)]


# --------------------------------------------------------------------------------------
# Construction
# --------------------------------------------------------------------------------------


def test_a_single_entry_tree_roots_at_its_leaf() -> None:
    tree = build_tree(["a" * 64])
    assert tree.size == 1
    assert tree.root == leaf_hash("a" * 64)


@pytest.mark.parametrize("count", [1, 2, 3, 4, 5, 7, 8, 9, 16, 17, 100])
def test_trees_of_any_size_build_and_produce_one_root(count: int) -> None:
    tree = build_tree(_hashes(count))
    assert tree.size == count
    assert len(tree.levels[-1]) == 1
    assert len(tree.root) == 64


def test_building_is_deterministic() -> None:
    entries = _hashes(11)
    assert merkle_root(entries) == merkle_root(list(entries))


def test_an_empty_batch_is_refused_rather_than_rooted_at_the_empty_hash() -> None:
    """Anchoring nothing would publish a commitment that proves nothing while looking like proof."""
    with pytest.raises(MerkleError, match="empty batch"):
        build_tree([])
    # The constant exists so a verifier meeting one can recognise it, not so we can produce it.
    assert hashlib.sha256(b"").hexdigest() == EMPTY_ROOT


# --------------------------------------------------------------------------------------
# The properties a naive implementation gets wrong
# --------------------------------------------------------------------------------------


def test_leaves_and_interior_nodes_are_domain_separated() -> None:
    """Second-preimage resistance: an interior node must not be presentable as a leaf.

    Without the 0x00/0x01 prefixes, a tree over two entries and a tree over their combined digest
    could produce the same root, so a proof of inclusion would prove nothing about *what kind* of
    thing was included.
    """
    a, b = _hashes(2)
    tree = build_tree([a, b])
    interior = tree.root
    # Feeding the interior digest back in as a leaf must not reproduce the same root.
    assert build_tree([interior]).root != interior
    # And the leaf hash of a value is not the bare hash of that value.
    assert leaf_hash(a) != hashlib.sha256(a.encode()).hexdigest()


def test_odd_nodes_are_promoted_not_duplicated() -> None:
    """CVE-2012-2459: duplicating the last node lets two different leaf sets share a root.

    With three leaves [a, b, c], a duplicating implementation computes the same root as the
    four-leaf set [a, b, c, c]. A promoting one does not, so these must differ.
    """
    a, b, c = _hashes(3)
    assert merkle_root([a, b, c]) != merkle_root([a, b, c, c])


def test_order_is_committed_to_not_just_membership() -> None:
    """The ledger's order *is* the evidence — a custody chain reordered is a different history."""
    a, b, c = _hashes(3)
    assert merkle_root([a, b, c]) != merkle_root([c, b, a])
    assert merkle_root([a, b, c]) != merkle_root([b, a, c])


def test_changing_any_entry_changes_the_root() -> None:
    entries = _hashes(9)
    baseline = merkle_root(entries)
    for index in range(len(entries)):
        mutated = list(entries)
        mutated[index] = "f" * 64
        assert merkle_root(mutated) != baseline, f"entry {index} did not affect the root"


def test_removing_the_tail_changes_the_root() -> None:
    """The whole point of anchoring: a truncated batch must not match the published root."""
    entries = _hashes(10)
    assert merkle_root(entries[:-1]) != merkle_root(entries)
    assert merkle_root(entries[:5]) != merkle_root(entries)


def test_appending_changes_the_root() -> None:
    entries = _hashes(6)
    assert merkle_root([*entries, "e" * 64]) != merkle_root(entries)


# --------------------------------------------------------------------------------------
# Inclusion proofs
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("count", [1, 2, 3, 4, 5, 8, 9, 13, 32])
def test_every_leaf_has_a_verifiable_proof(count: int) -> None:
    entries = _hashes(count)
    tree = build_tree(entries)
    for index in range(count):
        proof = inclusion_proof(tree, index)
        assert verify_inclusion(proof, tree.root), f"leaf {index} of {count} failed"


def test_a_proof_does_not_verify_against_a_different_root() -> None:
    tree = build_tree(_hashes(8))
    other = build_tree(_hashes(9))
    assert not verify_inclusion(inclusion_proof(tree, 3), other.root)


def test_a_proof_for_an_entry_not_in_the_tree_fails() -> None:
    tree = build_tree(_hashes(8))
    proof = inclusion_proof(tree, 3)
    forged = InclusionProof(index=proof.index, entry_hash="f" * 64, path=proof.path)
    assert not verify_inclusion(forged, tree.root)


def test_a_tampered_path_fails() -> None:
    tree = build_tree(_hashes(8))
    proof = inclusion_proof(tree, 3)
    swapped = InclusionProof(
        index=proof.index,
        entry_hash=proof.entry_hash,
        path=tuple(
            (sibling, "left" if side == "right" else "right") for sibling, side in proof.path
        ),
    )
    assert not verify_inclusion(swapped, tree.root)


def test_a_malformed_path_is_invalid_rather_than_an_error() -> None:
    """A proof that does not check out is a finding, not a fault."""
    bad = InclusionProof(index=0, entry_hash="a" * 64, path=(("not-hex", "left"),))
    assert not verify_inclusion(bad, "b" * 64)


def test_an_out_of_range_index_is_refused() -> None:
    tree = build_tree(_hashes(4))
    with pytest.raises(MerkleError, match="outside a tree"):
        inclusion_proof(tree, 4)
    with pytest.raises(MerkleError, match="outside a tree"):
        inclusion_proof(tree, -1)


def test_a_proof_discloses_only_siblings_not_other_entries() -> None:
    """Why proofs matter: a batch can span cases, and a court presentation must not leak the rest.

    The path is O(log n) sibling digests. None of them is another entry's hash in the clear.
    """
    entries = _hashes(16)
    tree = build_tree(entries)
    proof = inclusion_proof(tree, 5)
    assert len(proof.path) == 4  # log2(16)
    disclosed = {sibling for sibling, _ in proof.path}
    assert not disclosed.intersection(entries)
