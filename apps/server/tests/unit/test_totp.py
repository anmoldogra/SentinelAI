"""Unit tests for `platform.security.totp` — anchored on RFC 4226 and RFC 6238's own vectors.

The published vectors are the whole point of this file. A TOTP implementation that is
self-consistent but wrong is indistinguishable from a correct one under round-trip tests
(`compute_code` then `verify_code` agrees with itself either way) — and it fails only in
production, against real authenticator apps, as "the codes never work". Checking against the
numbers the RFCs print is the only test that catches that class of error.

Vectors:
  * RFC 4226 Appendix D — HOTP, 6 digits, counters 0-9.
  * RFC 6238 Appendix B — TOTP, **8 digits**, SHA-1 rows only (this module is SHA-1 only, and
    the RFC's SHA-256/512 rows use different, longer seeds).

Both use the ASCII seed "12345678901234567890"; base32-encoded, that is the constant below.
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta, timezone

import pytest

from sentinelai.platform.security.totp import (
    DEFAULT_PERIOD_SECONDS,
    InvalidTotpSecret,
    compute_code,
    current_step,
    generate_secret,
    provisioning_uri,
    verify_code,
)

_RFC_SEED = b"12345678901234567890"
_SECRET = base64.b32encode(_RFC_SEED).decode("ascii")


def _at(unix_seconds: int) -> datetime:
    return datetime.fromtimestamp(unix_seconds, tz=UTC)


# --- RFC 4226 Appendix D: HOTP, 6 digits ------------------------------------

_HOTP_VECTORS = [
    (0, "755224"),
    (1, "287082"),
    (2, "359152"),
    (3, "969429"),
    (4, "338314"),
    (5, "254676"),
    (6, "287922"),
    (7, "162583"),
    (8, "399871"),
    (9, "520489"),
]


@pytest.mark.parametrize(("counter", "expected"), _HOTP_VECTORS)
def test_matches_rfc4226_hotp_vectors(counter: int, expected: str) -> None:
    assert compute_code(_SECRET, counter, digits=6) == expected


# --- RFC 6238 Appendix B: TOTP, 8 digits, SHA-1 -----------------------------

_TOTP_VECTORS = [
    (59, "94287082"),
    (1111111109, "07081804"),
    (1111111111, "14050471"),
    (1234567890, "89005924"),
    (2000000000, "69279037"),
    (20000000000, "65353130"),
]


@pytest.mark.parametrize(("unix_seconds", "expected"), _TOTP_VECTORS)
def test_matches_rfc6238_totp_vectors(unix_seconds: int, expected: str) -> None:
    step = current_step(_at(unix_seconds), period=DEFAULT_PERIOD_SECONDS)
    assert compute_code(_SECRET, step, digits=8) == expected


@pytest.mark.parametrize(("unix_seconds", "expected"), _TOTP_VECTORS)
def test_rfc6238_vectors_verify(unix_seconds: int, expected: str) -> None:
    """The same vectors through the verification path, not just the generator."""
    at = _at(unix_seconds)
    assert verify_code(_SECRET, expected, at, digits=8) == current_step(at)


def test_rfc6238_time_step_values() -> None:
    """The RFC prints T alongside each code; check the counter itself, not only the digits."""
    assert current_step(_at(59)) == 0x1
    assert current_step(_at(1111111109)) == 0x23523EC
    assert current_step(_at(1111111111)) == 0x23523ED
    assert current_step(_at(1234567890)) == 0x273EF07
    assert current_step(_at(2000000000)) == 0x3F940AA
    assert current_step(_at(20000000000)) == 0x27BC86AA


# --- verification behaviour -------------------------------------------------


def test_verify_returns_the_matching_step_not_a_boolean() -> None:
    """The replay guard is built on this return value — a bool would make it unimplementable."""
    at = _at(1234567890)
    result = verify_code(_SECRET, compute_code(_SECRET, current_step(at)), at)
    assert result == current_step(at)
    assert not isinstance(result, bool)


def test_a_wrong_code_returns_none_rather_than_raising() -> None:
    assert verify_code(_SECRET, "000000", _at(1234567890)) is None


def test_code_from_the_previous_step_is_accepted_within_drift() -> None:
    at = _at(1234567890)
    previous = current_step(at) - 1
    assert verify_code(_SECRET, compute_code(_SECRET, previous), at) == previous


def test_code_from_the_next_step_is_accepted_within_drift() -> None:
    """Clock skew runs both ways; a client marginally ahead must still authenticate."""
    at = _at(1234567890)
    following = current_step(at) + 1
    assert verify_code(_SECRET, compute_code(_SECRET, following), at) == following


def test_code_outside_the_drift_window_is_rejected() -> None:
    at = _at(1234567890)
    stale = current_step(at) - 2
    assert verify_code(_SECRET, compute_code(_SECRET, stale), at) is None


def test_drift_can_be_disabled_entirely() -> None:
    at = _at(1234567890)
    previous = current_step(at) - 1
    assert verify_code(_SECRET, compute_code(_SECRET, previous), at, drift_steps=0) is None


def test_nearest_step_wins_when_several_would_match() -> None:
    """Ordering matters: the returned step is claimed against the replay guard, and claiming a
    step further from now than necessary would burn later codes."""
    at = _at(1234567890)
    centre = current_step(at)
    assert verify_code(_SECRET, compute_code(_SECRET, centre), at, drift_steps=5) == centre


def test_a_code_holds_for_its_whole_step_and_no_longer() -> None:
    start = _at(1234567890 - 1234567890 % DEFAULT_PERIOD_SECONDS)  # step boundary
    code = compute_code(_SECRET, current_step(start))
    assert verify_code(_SECRET, code, start, drift_steps=0) is not None
    last_moment = start + timedelta(seconds=DEFAULT_PERIOD_SECONDS - 1)
    assert verify_code(_SECRET, code, last_moment, drift_steps=0) is not None
    just_after = start + timedelta(seconds=DEFAULT_PERIOD_SECONDS)
    assert verify_code(_SECRET, code, just_after, drift_steps=0) is None


@pytest.mark.parametrize("presented", ["94287082 ", " 940 71", "abcdef", "", "12345", "1234567"])
def test_malformed_codes_are_rejected_without_raising(presented: str) -> None:
    assert verify_code(_SECRET, presented, _at(1234567890)) is None


def test_spaced_and_hyphenated_codes_are_accepted() -> None:
    """Authenticator apps display `123 456`; a user copying that must not be punished for it."""
    at = _at(1234567890)
    code = compute_code(_SECRET, current_step(at))
    assert verify_code(_SECRET, f"{code[:3]} {code[3:]}", at) == current_step(at)
    assert verify_code(_SECRET, f"{code[:3]}-{code[3:]}", at) == current_step(at)


# --- secret handling --------------------------------------------------------


def test_generated_secrets_are_160_bit_unpadded_base32() -> None:
    secret = generate_secret()
    assert len(secret) == 32
    assert "=" not in secret
    assert len(base64.b32decode(secret)) == 20


def test_generated_secrets_are_unique() -> None:
    assert len({generate_secret() for _ in range(64)}) == 64


def test_generated_secrets_are_usable_end_to_end() -> None:
    at = _at(1234567890)
    secret = generate_secret()
    assert verify_code(secret, compute_code(secret, current_step(at)), at) is not None


@pytest.mark.parametrize(
    "variant",
    [
        _SECRET.lower(),
        f"{_SECRET[:8]} {_SECRET[8:]}",
        f"{_SECRET[:8]}-{_SECRET[8:]}",
        f"  {_SECRET}  ",
    ],
)
def test_secret_shapes_a_human_actually_types_are_accepted(variant: str) -> None:
    assert compute_code(variant, 1, digits=8) == "94287082"


def test_unpadded_secrets_decode() -> None:
    """`b32decode` demands padding; a secret from a QR or a config file usually lacks it.

    `b"1234"` is chosen because its base32 form genuinely carries a `=` — stripping padding from
    a value that had none would make this test pass without exercising anything.
    """
    padded = base64.b32encode(b"1234").decode("ascii")
    assert padded.endswith("=")
    unpadded = padded.rstrip("=")
    assert compute_code(unpadded, 0) == compute_code(padded, 0)


@pytest.mark.parametrize("bad", ["", "   ", "not-base32!", "1234567890"])
def test_a_malformed_secret_raises(bad: str) -> None:
    """A bad secret is a server-side data fault, not a failed login — it must not read as one."""
    with pytest.raises(InvalidTotpSecret):
        compute_code(bad, 0)


def test_verify_propagates_a_malformed_secret_rather_than_returning_none() -> None:
    """Returning None here would report a corrupt stored secret as 'wrong code', sending an
    analyst to re-enter a code forever against an account that can never verify one."""
    at = _at(1234567890)
    with pytest.raises(InvalidTotpSecret):
        verify_code("not-base32!", "123456", at)


# --- argument validation ----------------------------------------------------


def test_naive_datetimes_are_rejected() -> None:
    """A naive datetime is read as local time, shifting every code by the UTC offset."""
    with pytest.raises(ValueError, match="timezone-aware"):
        current_step(datetime(2026, 1, 1))  # a naive datetime is exactly what is under test


def test_a_non_utc_timezone_yields_the_same_step_as_its_utc_instant() -> None:
    """The step is a function of the instant, not of how it is labelled."""
    utc = _at(1234567890)
    kolkata = utc.astimezone(timezone(timedelta(hours=5, minutes=30)))
    assert kolkata.hour != utc.hour  # genuinely a different wall clock
    assert current_step(kolkata) == current_step(utc)


@pytest.mark.parametrize("digits", [4, 5, 9, 0, -1])
def test_unsupported_digit_counts_are_rejected(digits: int) -> None:
    with pytest.raises(ValueError, match="digits"):
        compute_code(_SECRET, 0, digits=digits)


def test_negative_steps_are_rejected() -> None:
    with pytest.raises(ValueError, match="negative"):
        compute_code(_SECRET, -1)


def test_negative_drift_is_rejected() -> None:
    with pytest.raises(ValueError, match="negative"):
        verify_code(_SECRET, "123456", _at(1234567890), drift_steps=-1)


def test_a_non_positive_period_is_rejected() -> None:
    with pytest.raises(ValueError, match="positive"):
        current_step(_at(0), period=0)


def test_steps_near_the_epoch_do_not_go_negative() -> None:
    """Drift at T=0 would reach step -1; it must be skipped, not packed as unsigned."""
    assert verify_code(_SECRET, "000000", _at(0)) is None


# --- provisioning URI -------------------------------------------------------


def test_provisioning_uri_carries_issuer_in_both_places() -> None:
    """Older apps read the label prefix, newer ones the parameter; emitting one loses the other."""
    uri = provisioning_uri(_SECRET, account="analyst@agency.gov", issuer="SentinelAI")
    assert uri.startswith("otpauth://totp/SentinelAI%3Aanalyst%40agency.gov?")
    assert "issuer=SentinelAI" in uri


def test_provisioning_uri_states_the_parameters_apps_must_not_guess() -> None:
    uri = provisioning_uri(_SECRET, account="a@b.gov", issuer="SentinelAI")
    assert "algorithm=SHA1" in uri
    assert "digits=6" in uri
    assert "period=30" in uri
    assert f"secret={_SECRET}" in uri


def test_provisioning_uri_validates_the_secret_up_front() -> None:
    """Better to fail at enrolment than to print a QR that can never produce a working code."""
    with pytest.raises(InvalidTotpSecret):
        provisioning_uri("not-base32!", account="a@b.gov", issuer="SentinelAI")
