"""RFC 3161 timestamping — ADR-0003 §3, Wave 1.3c.

The positive test is nearly worthless on its own: any verifier that returns without raising passes
it, including one that checks nothing. What makes this file meaningful is that every check in
:func:`~sentinelai.platform.crypto.tsa.verify_timestamp_token` has a test that produces a token
wrong
in exactly *that* way and asserts the rejection. `tests/fixtures/fake_tsa.py` mints real CMS
``SignedData`` with real RSA signatures and a real certificate chain, so none of these assertions
are
vacuous.

No network. A test that reached a public TSA would be slow, flaky, and a silent egress path in a
build that is supposed to prove air-gapped operation is possible.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from cryptography.hazmat.primitives.serialization import Encoding

from sentinelai.platform.config import ConfigurationError, Settings
from sentinelai.platform.crypto.tsa import (
    EKU_TIME_STAMPING,
    TsaError,
    TsaVerificationError,
    build_timestamp_authority,
    build_timestamp_request,
    certificates_to_pem,
    load_trust_anchors,
    token_from_response,
    verify_timestamp_token,
)
from tests.fixtures.fake_tsa import FakeTsa

_ROOT = "9f3ac21b" * 8  # a plausible Merkle root


def _round_trip(tsa: FakeTsa, message: bytes = _ROOT.encode()) -> tuple[bytes, int]:
    request = build_timestamp_request(message)
    return token_from_response(tsa.timestamp(request.der)), request.nonce


# ---------------------------------------------------------------------------------------
# The lifecycle
# ---------------------------------------------------------------------------------------


def test_a_request_covers_the_message_digest_and_carries_a_nonce() -> None:
    """A request without a nonce could be answered by any previously-issued token for the digest."""
    first = build_timestamp_request(b"anchor-root")
    second = build_timestamp_request(b"anchor-root")

    assert first.hash_algo == "sha256"
    assert len(first.digest) == 32
    assert first.digest == second.digest  # same message, same imprint
    assert first.nonce != second.nonce  # fresh nonce per request
    assert first.der != second.der


def test_a_granted_response_verifies_end_to_end() -> None:
    tsa = FakeTsa()
    token, nonce = _round_trip(tsa)

    verified = verify_timestamp_token(
        token, message=_ROOT.encode(), trust_anchors=tsa.trust_anchors, nonce=nonce
    )

    assert verified.gen_time.tzinfo is not None
    assert verified.gen_time <= datetime.now(UTC) + timedelta(seconds=5)
    assert verified.serial_number > 0
    assert verified.hash_algo == "sha256"
    assert "SentinelAI Test TSA" in verified.signer_subject


def test_a_rejected_request_surfaces_the_failure_reason() -> None:
    """A real TSA rejection omits the token entirely, and the reason is the only useful content.

    asn1crypto declares `timeStampToken` required, deviating from RFC 3161 §2.4.2 — so a rejection
    parsed through its class raises, and the reason would be reported as "malformed response",
    sending an operator to debug the wrong thing. The module defines a lenient structure for this.
    """
    response = FakeTsa(status="rejection").timestamp(build_timestamp_request(b"x").der)

    with pytest.raises(TsaError) as excinfo:
        token_from_response(response)

    assert "refused" in str(excinfo.value)
    assert "bad_request" in str(excinfo.value)


def test_a_token_over_an_archived_root_verifies_without_a_nonce() -> None:
    """Re-verifying stored evidence years later is the primary use of this path.

    The nonce is not persisted with an anchor, so it cannot be re-checked. Requiring it would make
    every archived token unverifiable, which is the opposite of the point.
    """
    tsa = FakeTsa()
    token = tsa.token_for(_ROOT.encode())

    verified = verify_timestamp_token(
        token, message=_ROOT.encode(), trust_anchors=tsa.trust_anchors
    )

    assert verified.serial_number > 0


# ---------------------------------------------------------------------------------------
# What must be rejected — one test per check
# ---------------------------------------------------------------------------------------


def test_a_token_over_a_different_message_is_rejected() -> None:
    """The core binding: this token must cover *this* anchor's root and no other."""
    tsa = FakeTsa()
    token, nonce = _round_trip(tsa)

    with pytest.raises(TsaVerificationError, match="different digest"):
        verify_timestamp_token(
            token, message=b"a different root", trust_anchors=tsa.trust_anchors, nonce=nonce
        )


def test_a_replayed_nonce_is_rejected() -> None:
    """Replay protection: an old token for the same digest must not satisfy a fresh request."""
    tsa = FakeTsa()
    token, nonce = _round_trip(tsa)

    with pytest.raises(TsaVerificationError, match="nonce mismatch"):
        verify_timestamp_token(
            token, message=_ROOT.encode(), trust_anchors=tsa.trust_anchors, nonce=nonce + 1
        )


def test_a_tampered_signature_is_rejected() -> None:
    tsa = FakeTsa(corrupt_signature=True)
    token, nonce = _round_trip(tsa)

    with pytest.raises(TsaVerificationError, match="signature does not verify"):
        verify_timestamp_token(
            token, message=_ROOT.encode(), trust_anchors=tsa.trust_anchors, nonce=nonce
        )


def test_a_token_from_an_untrusted_authority_is_rejected() -> None:
    """Anyone can run a TSA. Only a configured one counts."""
    issuer = FakeTsa()
    stranger = FakeTsa()
    token, nonce = _round_trip(issuer)

    with pytest.raises(TsaVerificationError, match="does not chain"):
        verify_timestamp_token(
            token, message=_ROOT.encode(), trust_anchors=stranger.trust_anchors, nonce=nonce
        )


def test_an_empty_trust_store_refuses_rather_than_accepting_everything() -> None:
    """The failure mode that would quietly void the whole feature.

    A verifier that treated "no anchors configured" as "nothing to check against" would accept a
    token from any attacker while appearing to pass.
    """
    tsa = FakeTsa()
    token, nonce = _round_trip(tsa)

    with pytest.raises(TsaVerificationError, match="no TSA trust anchors"):
        verify_timestamp_token(token, message=_ROOT.encode(), trust_anchors=[], nonce=nonce)


def test_a_signer_without_the_timestamping_eku_is_rejected() -> None:
    """RFC 3161 §2.3. Without this check any certificate under the CA could attest to time."""
    tsa = FakeTsa(include_eku=False)
    token, nonce = _round_trip(tsa)

    with pytest.raises(TsaVerificationError, match="extendedKeyUsage"):
        verify_timestamp_token(
            token, message=_ROOT.encode(), trust_anchors=tsa.trust_anchors, nonce=nonce
        )


def test_a_signer_with_extra_eku_is_rejected() -> None:
    """RFC 3161 §2.3 requires timestamping to be the ONLY extended key usage.

    A certificate that can also do TLS server auth is a certificate whose key is exposed to a
    completely different threat model, and it must not be able to sign attestations about time.
    """
    tsa = FakeTsa(extra_eku=True)
    token, nonce = _round_trip(tsa)

    with pytest.raises(TsaVerificationError, match="and nothing else"):
        verify_timestamp_token(
            token, message=_ROOT.encode(), trust_anchors=tsa.trust_anchors, nonce=nonce
        )


def test_a_non_critical_eku_is_rejected() -> None:
    """RFC 3161 §2.3 requires the extension to be critical, so no verifier may ignore it."""
    tsa = FakeTsa(eku_critical=False)
    token, nonce = _round_trip(tsa)

    with pytest.raises(TsaVerificationError, match="critical"):
        verify_timestamp_token(
            token, message=_ROOT.encode(), trust_anchors=tsa.trust_anchors, nonce=nonce
        )


def test_a_token_signed_outside_the_certificates_validity_is_rejected() -> None:
    """A genTime outside the signer's validity window means the attestation is not trustworthy."""
    tsa = FakeTsa(certificate_expired=True)
    token, nonce = _round_trip(tsa)

    with pytest.raises(TsaVerificationError, match="validity"):
        verify_timestamp_token(
            token, message=_ROOT.encode(), trust_anchors=tsa.trust_anchors, nonce=nonce
        )


def test_a_token_carrying_no_certificates_is_rejected() -> None:
    """The request sets cert_req=True precisely so the token is self-verifiable."""
    tsa = FakeTsa(omit_certificates=True)
    token, nonce = _round_trip(tsa)

    with pytest.raises(TsaVerificationError, match="no certificates"):
        verify_timestamp_token(
            token, message=_ROOT.encode(), trust_anchors=tsa.trust_anchors, nonce=nonce
        )


def test_garbage_bytes_are_rejected_rather_than_crashing() -> None:
    """Hostile input must produce a verification failure, not an unhandled parse error."""
    tsa = FakeTsa()

    with pytest.raises(TsaVerificationError, match="not well-formed"):
        verify_timestamp_token(
            b"\x30\x82not-a-token", message=_ROOT.encode(), trust_anchors=tsa.trust_anchors
        )


def test_sha1_imprints_are_refused_outright() -> None:
    """A timestamp over a collidable digest attests to nothing, even if the token verifies."""
    with pytest.raises(TsaError, match="unsupported timestamp digest"):
        build_timestamp_request(b"root", hash_algo="sha1")


# ---------------------------------------------------------------------------------------
# Trust anchor plumbing and the air-gapped fallback
# ---------------------------------------------------------------------------------------


def test_trust_anchors_round_trip_through_pem() -> None:
    """How a deployment actually supplies its TSA roots — one PEM blob in configuration."""
    tsa = FakeTsa()

    loaded = load_trust_anchors(tsa.trust_anchor_pem)
    assert len(loaded) == 1
    assert loaded[0].subject == tsa.trust_anchors[0].subject

    reloaded = load_trust_anchors(certificates_to_pem(loaded))
    assert reloaded[0].public_bytes(Encoding.DER) == loaded[0].public_bytes(Encoding.DER)


def test_an_empty_pem_bundle_loads_as_no_anchors() -> None:
    """A deployment with no TSA legitimately has none; refusal belongs in the verifier, not here."""
    assert load_trust_anchors("") == []
    assert load_trust_anchors("   \n  ") == []


def test_timestamping_disabled_yields_no_authority() -> None:
    """The air-gapped configuration, expressed as absence rather than a flag to branch on."""
    assert build_timestamp_authority(enabled=False, url="", trust_anchors_pem="") is None
    # Even with a URL configured, `enabled=False` must win — a stale URL must not create egress.
    assert (
        build_timestamp_authority(enabled=False, url="http://tsa.example/tsr", trust_anchors_pem="")
        is None
    )


def test_an_enabled_authority_requires_a_url() -> None:
    with pytest.raises(TsaError, match="TSA URL is required"):
        build_timestamp_authority(enabled=True, url="", trust_anchors_pem="")


def test_an_air_gapped_profile_refuses_to_start_with_timestamping_enabled() -> None:
    """The hard invariant: an air-gapped deployment must have no configured egress path.

    Checked for every profile, not only production — a developer running the air-gapped profile is
    usually doing so to prove the absence of egress, and a silently ignored TSA URL would make that
    exercise worthless.
    """
    for profile in ("air-gapped", "classified"):
        settings = Settings(app_env=profile, tsa_enabled=True, tsa_url="http://tsa.example/tsr")
        with pytest.raises(ConfigurationError, match="no configured egress"):
            settings.validate_for_profile()


def test_an_air_gapped_profile_starts_cleanly_with_timestamping_disabled() -> None:
    """WORM anchoring needs no network beyond the object store: air-gapped stays supported."""
    settings = Settings(
        app_env="air-gapped",
        tsa_enabled=False,
        kms_provider="vault_transit",
        malware_scanner_provider="clamav",
        storage_access_key="real-key",
        storage_secret_key="real-secret",
    )
    settings.validate_for_profile()  # must not raise


def test_production_refuses_timestamping_enabled_without_a_trust_store() -> None:
    """Enabled-but-unverifiable is the worst of both worlds: egress opened, nothing provable."""
    settings = Settings(
        app_env="production",
        tsa_enabled=True,
        tsa_url="http://tsa.example/tsr",
        tsa_trust_anchors_pem="",
        kms_provider="vault_transit",
        malware_scanner_provider="clamav",
        storage_access_key="real-key",
        storage_secret_key="real-secret",
    )
    with pytest.raises(ConfigurationError, match="TSA_TRUST_ANCHORS_PEM"):
        settings.validate_for_profile()


def test_the_timestamping_eku_oid_is_the_rfc_value() -> None:
    """Pinned because a typo here would silently accept every certificate."""
    assert EKU_TIME_STAMPING == "1.3.6.1.5.5.7.3.8"
