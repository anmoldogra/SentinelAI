"""TOTP (RFC 6238) over HOTP (RFC 4226) — the offline second factor.

`security-architecture.md` §8 makes MFA mandatory for every role that can reach evidence or case
data, and lists TOTP as the **required minimum** because it is the only accepted factor that works
with zero connectivity — which the `air-gapped` and `classified` profiles require. ADR-0010 A1
scopes the current increment to it.

**Pure by design: no database, no KMS, no clock of its own.** Every function takes what it needs
as an argument, so the whole module is testable against RFC 6238's published vectors with no
fixtures. Persistence (the encrypted secret, the replay guard) lives in `auth/repository.py`;
deciding *whether* a login proceeds lives in the service. This file only answers "does this code
match this secret at this instant".

**SHA-1 only.** RFC 6238 permits SHA-256/512, but every mainstream authenticator app implements
SHA-1 and silently misreads a provisioning URI that asks for anything else — an interoperability
failure that presents as "the app's codes never work". The construction's security does not rest
on the hash's collision resistance (RFC 4226 §7.2), so this is not the weakness it looks like.

Distinct from `security.hashing` (argon2id password KDF) and `security.digest` (evidence content
hashing): this is a shared-secret one-time-password algorithm and is used for nothing else.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import secrets
import struct
from datetime import datetime
from urllib.parse import quote, urlencode

# 160 bits, the RFC 4226 §4 R6 recommendation and what authenticator apps expect. 20 bytes encodes
# to exactly 32 base32 characters, so a generated secret never carries `=` padding.
_SECRET_BYTES = 20
# RFC 6238 §4.1's default time step.
DEFAULT_PERIOD_SECONDS = 30
DEFAULT_DIGITS = 6
_ALLOWED_DIGITS = frozenset({6, 7, 8})
# RFC 6238 §5.2: "we RECOMMEND that at most one time step is allowed as the network delay."
DEFAULT_DRIFT_STEPS = 1


class InvalidTotpSecret(ValueError):
    """The shared secret is not decodable base32."""


def generate_secret() -> str:
    """Return a fresh 160-bit shared secret, base32-encoded and unpadded."""
    return base64.b32encode(secrets.token_bytes(_SECRET_BYTES)).decode("ascii")


def _decode_secret(secret: str) -> bytes:
    """Decode a base32 shared secret, tolerating the shapes humans and apps actually produce.

    Case-insensitive, whitespace-stripped, and padding-tolerant: a secret transcribed from a
    provisioning QR or typed in by hand arrives lowercased, space-grouped, or with its `=` padding
    dropped, and none of those are the user's mistake to pay for.
    """
    normalized = secret.replace(" ", "").replace("-", "").strip().upper()
    if not normalized:
        raise InvalidTotpSecret("secret is empty")
    # b32decode demands padding to a multiple of 8 characters.
    padded = normalized + "=" * (-len(normalized) % 8)
    try:
        return base64.b32decode(padded, casefold=True)
    except (binascii.Error, ValueError) as exc:
        raise InvalidTotpSecret("secret is not valid base32") from exc


def current_step(at: datetime, *, period: int = DEFAULT_PERIOD_SECONDS) -> int:
    """Return RFC 6238's time-step counter `T` for ``at``.

    **``at`` must be timezone-aware.** A naive datetime's ``timestamp()`` is interpreted in the
    host's local zone, which would silently shift every code by the UTC offset — codes that verify
    on a UTC server and fail on a developer's laptop. Rejecting it is cheaper than debugging it.
    """
    if at.tzinfo is None:
        raise ValueError("`at` must be timezone-aware; a naive datetime is read as local time")
    if period <= 0:
        raise ValueError("`period` must be positive")
    return int(at.timestamp()) // period


def compute_code(secret: str, step: int, *, digits: int = DEFAULT_DIGITS) -> str:
    """Return the HOTP value (RFC 4226 §5.3) for ``secret`` at counter ``step``, zero-padded."""
    if digits not in _ALLOWED_DIGITS:
        raise ValueError(f"`digits` must be one of {sorted(_ALLOWED_DIGITS)}")
    if step < 0:
        raise ValueError("`step` must not be negative")

    digest = hmac.new(_decode_secret(secret), struct.pack(">Q", step), hashlib.sha1).digest()
    # Dynamic truncation (RFC 4226 §5.4): the low nibble of the last byte selects a 4-byte window,
    # whose top bit is masked off so the result is sign-agnostic across implementations.
    offset = digest[-1] & 0x0F
    truncated = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(truncated % (10**digits)).zfill(digits)


def verify_code(
    secret: str,
    code: str,
    at: datetime,
    *,
    digits: int = DEFAULT_DIGITS,
    period: int = DEFAULT_PERIOD_SECONDS,
    drift_steps: int = DEFAULT_DRIFT_STEPS,
) -> int | None:
    """Return the time step ``code`` matched, or ``None`` if it matched none.

    **Returns the step, not a boolean, and that is the point.** RFC 6238 §5.2 requires a verifier
    to reject a code it has already accepted, which is only possible if the caller learns *which*
    step was satisfied — the value it then claims via
    ``MfaRepository.claim_totp_step``. A ``bool`` here would make the replay check unimplementable.

    Candidate steps are tried nearest-first, so the returned step is the closest match to ``at``
    when the drift window admits more than one.

    A wrong code is a ``None`` return, never an exception; only a malformed *secret* raises
    (:class:`InvalidTotpSecret`), because that is a server-side data fault rather than a failed
    authentication attempt.
    """
    if drift_steps < 0:
        raise ValueError("`drift_steps` must not be negative")

    candidate = code.replace(" ", "").replace("-", "").strip()
    if not candidate.isdigit() or len(candidate) != digits:
        # Rejected on shape before any HMAC work. This leaks only the expected code *length*,
        # which the provisioning URI publishes anyway.
        return None

    centre = current_step(at, period=period)
    # 0, -1, +1, -2, +2, … — nearest first.
    offsets = [0]
    for distance in range(1, drift_steps + 1):
        offsets.extend((-distance, distance))

    for offset in offsets:
        step = centre + offset
        if step < 0:
            continue
        if hmac.compare_digest(compute_code(secret, step, digits=digits), candidate):
            return step
    return None


def provisioning_uri(
    secret: str,
    *,
    account: str,
    issuer: str,
    digits: int = DEFAULT_DIGITS,
    period: int = DEFAULT_PERIOD_SECONDS,
) -> str:
    """Return the ``otpauth://`` URI an authenticator app consumes, usually via a QR code.

    The label repeats the issuer as a prefix (`issuer:account`) *and* passes it as a parameter:
    Key Uri Format specifies both, older apps read only the prefix, and newer ones prefer the
    parameter. Emitting one alone leaves some app showing the account with no idea which system it
    belongs to — which matters when an analyst holds credentials for several.

    The secret is validated here rather than trusted, so a malformed one fails at enrolment
    instead of becoming a QR code that can never produce a working code.
    """
    _decode_secret(secret)
    label = quote(f"{issuer}:{account}", safe="")
    params = urlencode(
        {
            "secret": secret,
            "issuer": issuer,
            "algorithm": "SHA1",
            "digits": digits,
            "period": period,
        }
    )
    return f"otpauth://totp/{label}?{params}"


__all__ = [
    "DEFAULT_DIGITS",
    "DEFAULT_DRIFT_STEPS",
    "DEFAULT_PERIOD_SECONDS",
    "InvalidTotpSecret",
    "compute_code",
    "current_step",
    "generate_secret",
    "provisioning_uri",
    "verify_code",
]
