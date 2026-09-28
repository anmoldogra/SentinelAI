"""Opaque bearer-token generation primitive (ADR-0010 §1-2, security-architecture §35).

Sessions are opaque server-side secrets: a high-entropy token is issued to the client and the store
keeps only its **hash** (via ``PasswordHasher`` / a keyed HMAC — ADR-0009), never the token itself.
This module generates the token and its short, indexable **lookup prefix** (ADR-0010 §1's
``token_lookup`` prefix, used to find the candidate row before verifying the full-token hash).

All randomness here is cryptographically secure (``secrets``) — the ``random`` module must never be
used for a security value.
"""

from __future__ import annotations

import secrets

# 32 bytes = 256 bits of entropy; token_urlsafe(32) yields a ~43-char URL-safe string.
_TOKEN_BYTES = 32
# A short, non-secret prefix stored in an indexed column for an O(1) session lookup without
# scanning on (or exposing) the secret itself. Security rests on the full-token hash, not this.
LOOKUP_PREFIX_LENGTH = 12
# Crockford-style base32 minus the confusable letters. A recovery code is transcribed by hand from
# a screen to paper and back, so an alphabet containing both `0` and `O` guarantees support tickets.
_RECOVERY_ALPHABET = "ABCDEFGHJKMNPQRSTVWXYZ23456789"
_RECOVERY_CODE_LENGTH = 10


def generate_opaque_token() -> str:
    """Return a fresh, URL-safe, 256-bit opaque bearer token (cryptographically random)."""
    return secrets.token_urlsafe(_TOKEN_BYTES)


def token_lookup_prefix(token: str) -> str:
    """Return the short indexable lookup prefix of ``token`` (not a secret on its own)."""
    return token[:LOOKUP_PREFIX_LENGTH]


def generate_recovery_code() -> str:
    """Return one MFA recovery code — ``security-architecture.md`` §8's fallback factor.

    Shaped for a human to write down and type back, which is the whole point of a recovery code:
    Crockford-style base32 (no ``I``/``L``/``O``/``U``, so nothing is confusable with ``1``/``0``
    or accidentally profane), upper case, in two hyphenated groups.

    Ten characters of this alphabet is ~51 bits. Less than a session token, and deliberately so —
    a longer code gets transcribed wrong, and the code is additionally rate-limited by the
    argon2id verify each attempt costs and by being single-use. It is generated with ``secrets``
    like everything else here.
    """
    raw = "".join(secrets.choice(_RECOVERY_ALPHABET) for _ in range(_RECOVERY_CODE_LENGTH))
    return f"{raw[:5]}-{raw[5:]}"
