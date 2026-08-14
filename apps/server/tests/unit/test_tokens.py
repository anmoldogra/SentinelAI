"""Opaque bearer-token primitive tests (W1-08, ADR-0010 §1-2)."""

from __future__ import annotations

import string

from sentinelai.platform.security.tokens import (
    LOOKUP_PREFIX_LENGTH,
    generate_opaque_token,
    token_lookup_prefix,
)

_URLSAFE = set(string.ascii_letters + string.digits + "-_")


def test_token_is_url_safe_and_high_entropy() -> None:
    token = generate_opaque_token()
    # token_urlsafe(32) → 256 bits → ~43 url-safe chars.
    assert len(token) >= 43
    assert set(token) <= _URLSAFE


def test_tokens_are_unique() -> None:
    tokens = {generate_opaque_token() for _ in range(1000)}
    assert len(tokens) == 1000  # no collisions across many draws


def test_lookup_prefix_is_the_leading_slice() -> None:
    token = generate_opaque_token()
    prefix = token_lookup_prefix(token)
    assert prefix == token[:LOOKUP_PREFIX_LENGTH]
    assert len(prefix) == LOOKUP_PREFIX_LENGTH
    assert token.startswith(prefix)
