"""Argon2 password-hashing primitive tests (W1-08, ADR-0010 §3)."""

from __future__ import annotations

from argon2 import PasswordHasher as _Argon2Hasher

from sentinelai.platform.security.hashing import Argon2PasswordHasher


def test_hash_is_argon2id_and_not_the_plaintext() -> None:
    hasher = Argon2PasswordHasher()
    digest = hasher.hash("correct horse battery staple")
    assert digest.startswith("$argon2id$")  # argon2id variant (ADR-0010)
    assert "correct horse battery staple" not in digest  # never embeds the plaintext


def test_hash_is_salted_so_two_hashes_differ() -> None:
    hasher = Argon2PasswordHasher()
    assert hasher.hash("same-password") != hasher.hash("same-password")


def test_verify_accepts_correct_and_rejects_wrong() -> None:
    hasher = Argon2PasswordHasher()
    digest = hasher.hash("s3cret")
    assert hasher.verify(digest, "s3cret") is True
    assert hasher.verify(digest, "s3cre7") is False


def test_verify_returns_false_on_malformed_hash_without_raising() -> None:
    hasher = Argon2PasswordHasher()
    assert hasher.verify("not-a-valid-argon2-hash", "anything") is False


def test_needs_rehash_true_for_weaker_params_false_for_current() -> None:
    hasher = Argon2PasswordHasher()
    weak = _Argon2Hasher(time_cost=1, memory_cost=8, parallelism=1).hash("s3cret")
    assert hasher.needs_rehash(weak) is True
    assert hasher.needs_rehash(hasher.hash("s3cret")) is False


def test_parameters_are_injectable() -> None:
    # A configured argon2 hasher can be injected (security-architecture posture tuning).
    strong = Argon2PasswordHasher(_Argon2Hasher(time_cost=4))
    digest = strong.hash("s3cret")
    assert strong.verify(digest, "s3cret") is True
