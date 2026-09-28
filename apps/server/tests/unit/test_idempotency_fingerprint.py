"""The fingerprint decides replay-versus-conflict — api-design.md §2.9, ADR-0012 §2.

Pure tests: no database, no ASGI scope, no clock. Every property here is a property of the hash
preimage, and getting one wrong means either replaying a response for a request that was not the
same one (the dangerous direction) or refusing a legitimate retry (the annoying one).
"""

from __future__ import annotations

import hashlib

from sentinelai.platform.idempotency.fingerprint import (
    FINGERPRINT_ALGORITHM,
    request_fingerprint,
)

_PRINCIPAL = "8b1a9953-1234-4c5d-9abc-1f2e3d4c5b6a"
_OTHER_PRINCIPAL = "2c9e6f77-4321-4d5e-8fab-6a5b4c3d2e1f"


def _fp(**overrides: object) -> str:
    kwargs: dict[str, object] = {
        "method": "POST",
        "path": "/api/v1/evidence",
        "principal_id": _PRINCIPAL,
        "body": b'{"title":"seized laptop"}',
    }
    kwargs.update(overrides)
    return request_fingerprint(**kwargs)  # type: ignore[arg-type]


def test_the_same_request_fingerprints_identically() -> None:
    """The property the whole feature rests on: a retry must be recognisable as one."""
    assert _fp() == _fp()


def test_it_is_a_sha256_hex_digest() -> None:
    value = _fp()
    assert len(value) == 64
    assert int(value, 16) >= 0, "hex, so it is comparable and loggable as text"
    assert FINGERPRINT_ALGORITHM == "SHA-256"


def test_a_different_body_changes_it() -> None:
    """api-design.md §2.9's conflict condition — same key, different body."""
    assert _fp() != _fp(body=b'{"title":"seized phone"}')


def test_a_whitespace_only_body_change_still_changes_it() -> None:
    """Deliberate: the body is hashed verbatim, not canonicalized.

    Replaying for semantically-equal-but-textually-different JSON would mean parsing
    attacker-controlled input before any handler has validated it, to buy a friendlier answer in a
    case where the unfriendly answer (conflict → the client retries fresh) is already safe.
    """
    assert _fp(body=b'{"title":"x"}') != _fp(body=b'{"title": "x"}')


def test_an_empty_body_is_fingerprintable() -> None:
    """A keyed POST with no body is legitimate — it must get a stable fingerprint, not an error."""
    assert _fp(body=b"") == _fp(body=b"")
    assert _fp(body=b"") != _fp()


def test_an_unparseable_body_is_fingerprintable() -> None:
    """The guard runs before validation, so it must not assume the body is JSON at all."""
    assert _fp(body=b"\xff\xfe not json") == _fp(body=b"\xff\xfe not json")


def test_the_method_is_covered() -> None:
    """`PUT` and `PATCH` on one path with one body are different operations, and a key reused
    across them is the mistake §2.9 exists to catch."""
    assert _fp(method="PUT") != _fp(method="PATCH")


def test_the_method_is_case_insensitive() -> None:
    """HTTP methods are uppercase by spec; a client sending `post` means the same request."""
    assert _fp(method="post") == _fp(method="POST")


def test_the_path_is_covered() -> None:
    assert _fp(path="/api/v1/cases") != _fp(path="/api/v1/evidence")


def test_the_principal_is_covered() -> None:
    """Defence in depth. Keys are already scoped per principal by the unique constraint, so this
    cannot matter today — it matters the day the lookup is widened (a service account acting for a
    user), when a fingerprint that ignored identity would replay one caller's response to another.
    """
    assert _fp(principal_id=_OTHER_PRINCIPAL) != _fp()


def test_field_boundaries_are_unambiguous() -> None:
    """The separator is what stops one field's tail from reading as the next field's head.

    Without it, `("POST", "/a/b")` and `("POST/a", "/b")` would hash alike — the same
    domain-separation argument ADR-0003 §2 makes for the ledger preimage.
    """
    assert _fp(method="POST", path="/a/b") != _fp(method="POST/a", path="/b")


def test_a_body_cannot_impersonate_a_later_field() -> None:
    """The body is hashed last, so nothing follows it to be spoofed — but a body containing the
    separator byte must still not be able to restructure the preimage."""
    assert _fp(body=b"\x00" + _PRINCIPAL.encode()) != _fp(body=_PRINCIPAL.encode())


def test_the_preimage_is_the_documented_one() -> None:
    """Pins the exact construction. A refactor that reordered the fields or dropped the separator
    would still pass every test above by symmetry; this one fails, which is the point — the stored
    fingerprints of every live key are computed by the current code, and changing it silently
    invalidates them all.
    """
    expected = hashlib.sha256(
        b"POST\x00" + b"/api/v1/evidence\x00" + _PRINCIPAL.encode() + b"\x00" + b'{"a":1}'
    ).hexdigest()
    assert _fp(body=b'{"a":1}') == expected
