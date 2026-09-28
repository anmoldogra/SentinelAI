"""The request fingerprint — what makes a retry "the same request" (api-design.md §2.9).

§2.9 stores ``(key, request body hash, response)`` and conflicts when the same key arrives with a
*different* body. This module decides what "different" means, and the answer is deliberately wider
than the body alone.

**Pure by design:** no session, no request object, no clock. It takes the four values it hashes and
returns hex, so every property below is testable without a database or an ASGI scope.
"""

from __future__ import annotations

import hashlib
from typing import Final

# The same algorithm the evidentiary subsystem uses (ADR-0003 §5's `hash_algo`). A fingerprint is
# not an evidentiary artifact — it decides replay-versus-conflict, not admissibility — but there is
# no reason to introduce a second hash into a codebase that already standardized on one.
FINGERPRINT_ALGORITHM: Final = "SHA-256"

# A byte that cannot appear in a URL path, an HTTP method, or hex, so the joined preimage cannot be
# ambiguous. `("POST", "/a/b")` and `("POST/a", "/b")` must not hash alike — the same domain-
# separation argument the ledger preimage makes (ADR-0003 §2), for the same reason.
_SEPARATOR: Final = b"\x00"


def request_fingerprint(*, method: str, path: str, principal_id: str, body: bytes) -> str:
    """Return the hex SHA-256 fingerprint of one request's identity.

    **Why the method and path are inside it, not just the body.** The unique constraint already
    scopes a key to a ``(principal, key, path)``, so a differing path cannot collide. The method
    can: ``PUT`` and ``PATCH`` on the same path with the same body are different operations, and a
    client reusing a key across them is making the mistake §2.9 exists to catch. Including both
    also means the stored fingerprint is self-describing — a support query can tell what a row is
    about without joining anything.

    **Why the principal is inside it as well**, given that keys are already scoped per principal:
    defence in depth on the one failure that matters here. If a future change ever widened the
    lookup — a service account acting for a user, say — a fingerprint that did not cover identity
    would let one caller's response replay for another. Hashing it costs nothing and closes that
    door before it is opened.

    **The body is hashed verbatim**, not canonicalized. JCS (ADR-0003 §2) would let a client resend
    semantically-identical JSON with different whitespace and still replay, which sounds friendlier
    and is wrong twice: a mismatch is the *safe* answer (the client retries and gets a fresh,
    correct result), and canonicalizing means parsing attacker-controlled input on the idempotency
    path before any handler has validated it. A body that will not parse still gets a fingerprint.
    """
    digest = hashlib.sha256()
    for part in (
        method.upper().encode("ascii"),
        path.encode("utf-8"),
        principal_id.encode("ascii"),
    ):
        digest.update(part)
        digest.update(_SEPARATOR)
    digest.update(body)
    return digest.hexdigest()


__all__ = ["FINGERPRINT_ALGORITHM", "request_fingerprint"]
