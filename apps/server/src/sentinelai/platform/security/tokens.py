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


def generate_opaque_token() -> str:
    """Return a fresh, URL-safe, 256-bit opaque bearer token (cryptographically random)."""
    return secrets.token_urlsafe(_TOKEN_BYTES)


def token_lookup_prefix(token: str) -> str:
    """Return the short indexable lookup prefix of ``token`` (not a secret on its own)."""
    return token[:LOOKUP_PREFIX_LENGTH]
