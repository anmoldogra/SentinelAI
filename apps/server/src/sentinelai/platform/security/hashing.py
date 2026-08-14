"""Password hashing primitive (ADR-0010 §3, guide Part 8, security-architecture §).

argon2id is the mandated password KDF (ADR-0010). A ``PasswordHasher`` port keeps callers (the
later auth/session code) depending on the *capability*, not on ``argon2-cffi`` directly, so the
algorithm/parameters can move in one place. The adapter never stores or logs a plaintext password;
``needs_rehash`` lets the login path transparently upgrade a stored hash when the cost parameters
are strengthened.

Hashing is deliberately slow — never call it on a hot path other than login/credential set.
"""

from __future__ import annotations

from typing import Protocol

from argon2 import PasswordHasher as _Argon2Hasher
from argon2.exceptions import InvalidHashError, VerificationError


class PasswordHasher(Protocol):
    """Port: hash a password, verify a candidate, and detect when a stored hash is stale."""

    def hash(self, password: str) -> str: ...

    def verify(self, stored_hash: str, candidate: str) -> bool: ...

    def needs_rehash(self, stored_hash: str) -> bool: ...


class Argon2PasswordHasher:
    """argon2id adapter (``argon2-cffi``).

    ``argon2-cffi``'s ``PasswordHasher`` defaults to the argon2id variant; parameters follow its
    RFC 9106-aligned defaults and are tuned per ``security-architecture.md``'s posture by injecting
    a configured ``argon2.PasswordHasher``.
    """

    def __init__(self, hasher: _Argon2Hasher | None = None) -> None:
        self._hasher = hasher if hasher is not None else _Argon2Hasher()

    def hash(self, password: str) -> str:
        """Return an argon2id hash string (embeds the salt + parameters); never the password."""
        return self._hasher.hash(password)

    def verify(self, stored_hash: str, candidate: str) -> bool:
        """Constant-time verify. Returns ``False`` on mismatch or malformed hash; never raises."""
        try:
            return self._hasher.verify(stored_hash, candidate)
        except (VerificationError, InvalidHashError):
            return False

    def needs_rehash(self, stored_hash: str) -> bool:
        """True when ``stored_hash`` was produced with weaker parameters and should be re-hashed."""
        return self._hasher.check_needs_rehash(stored_hash)
