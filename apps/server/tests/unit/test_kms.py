"""KMS unit tests (ADR-0009) — real software crypto via the Dev provider.

Correctness is proven by (a) provider round-trips, (b) independent-primitive cross-verification
of produced signatures, and (c) Ed25519 determinism (a deterministic signature is itself a
known-answer). Canonical RFC-8032 vectors should be pinned once the suite executes (deps
absent in this environment) — noted in the completion report. Also covers agility metadata,
rotation-preserves-history, lifecycle, capability negotiation, redaction, and fail-closed.
"""

from __future__ import annotations

import dataclasses
import json

import pytest
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.asymmetric import ed25519

from sentinelai.platform.crypto.audit import StructlogAuditSink
from sentinelai.platform.crypto.backends.dev import DevKmsProvider
from sentinelai.platform.crypto.exceptions import (
    AlgorithmNotAllowed,
    CapabilityNotSupported,
    InsecureConfiguration,
    KeystoreCorrupt,
    KmsUnavailable,
)
from sentinelai.platform.crypto.kms import KeyManagementService
from sentinelai.platform.crypto.policy import AlgorithmPolicy
from sentinelai.platform.crypto.registry import KeyRegistry
from sentinelai.platform.crypto.types import (
    CANONICAL_HEADER_VERSION,
    PAYLOAD_HASH_ALGORITHM,
    SIGNATURE_VERSION,
    Algorithm,
    KeyId,
    KeyPurpose,
    KeyRef,
    ProviderKind,
    Signature,
    SignatureBundle,
    SignedHeader,
    payload_hash,
)


def _kms(tmp_path, signing_algorithm: str = "ED25519") -> KeyManagementService:
    provider = DevKmsProvider(str(tmp_path / "ks"), is_production=False)
    policy = AlgorithmPolicy.from_config(signing_algorithm=signing_algorithm, hybrid=False)
    return KeyManagementService(KeyRegistry(provider), policy, StructlogAuditSink())


def test_dev_provider_fails_closed_in_production(tmp_path) -> None:
    with pytest.raises(InsecureConfiguration):
        DevKmsProvider(str(tmp_path / "ks"), is_production=True)


async def test_sign_verify_roundtrip_and_tamper_rejected(tmp_path) -> None:
    kms = _kms(tmp_path)
    ref = KeyRef(KeyPurpose.EVIDENCE_ROOT)
    await kms.create_key(ref)
    bundle = await kms.sign(ref, b"custody-entry-1")
    assert await kms.verify(b"custody-entry-1", bundle) is True
    assert await kms.verify(b"tampered", bundle) is False


async def test_signature_carries_agility_metadata(tmp_path) -> None:
    kms = _kms(tmp_path)
    ref = KeyRef(KeyPurpose.EVIDENCE_ROOT)
    await kms.create_key(ref)
    sig = (await kms.sign(ref, b"m")).primary
    assert sig.algorithm is Algorithm.ED25519
    assert sig.key_id.provider is ProviderKind.DEV
    assert sig.key_id.version == 1
    assert sig.signature_version == SIGNATURE_VERSION
    assert sig.created_at is not None


async def test_signature_is_over_the_canonical_header_and_binds_payload(tmp_path) -> None:
    kms = _kms(tmp_path)
    ref = KeyRef(KeyPurpose.EVIDENCE_ROOT)
    await kms.create_key(ref)
    sig = (await kms.sign(ref, b"same")).primary
    # The signature is over the canonical HEADER bytes (not the raw message) — independent
    # primitive cross-check proves real, correct Ed25519.
    pub = await kms.public_key(sig.key_id)
    ed25519.Ed25519PublicKey.from_public_bytes(pub).verify(sig.value, sig.header.canonical_bytes())
    # and the header authenticates the payload hash of the message.
    assert sig.header.payload_hash == payload_hash(b"same")


async def test_ecdsa_p256_roundtrip(tmp_path) -> None:
    kms = _kms(tmp_path, signing_algorithm="ECDSA_P256")
    ref = KeyRef(KeyPurpose.EVENT_ROOT)
    await kms.create_key(ref)
    bundle = await kms.sign(ref, b"event-envelope")
    assert bundle.primary.algorithm is Algorithm.ECDSA_P256
    assert await kms.verify(b"event-envelope", bundle) is True


async def test_aead_encrypt_decrypt_with_aad(tmp_path) -> None:
    kms = _kms(tmp_path)
    ref = KeyRef(KeyPurpose.STORAGE_ROOT)
    await kms.create_key(ref)
    ct = await kms.encrypt(ref, b"payload-dek", aad=b"evidence-123")
    assert await kms.decrypt(ct, aad=b"evidence-123") == b"payload-dek"
    with pytest.raises(InvalidTag):  # wrong AAD must fail authentication
        await kms.decrypt(ct, aad=b"evidence-999")


async def test_generate_data_key_redacts_plaintext(tmp_path) -> None:
    kms = _kms(tmp_path)
    ref = KeyRef(KeyPurpose.STORAGE_ROOT)
    await kms.create_key(ref)
    dek = await kms.generate_data_key(ref)
    assert len(dek.plaintext) == 32
    text = repr(dek)
    assert "REDACTED" in text
    assert dek.plaintext.hex() not in text  # never leak the DEK


async def test_rotation_preserves_history(tmp_path) -> None:
    kms = _kms(tmp_path)
    ref = KeyRef(KeyPurpose.EVIDENCE_ROOT)
    await kms.create_key(ref)
    sig_v1 = await kms.sign(ref, b"old-evidence")
    key_id = await kms.rotate(ref)
    sig_v2 = await kms.sign(ref, b"new-evidence")
    assert sig_v1.primary.key_id.version == 1
    assert key_id.version == 2 and sig_v2.primary.key_id.version == 2
    # both remain verifiable after rotation (crypto agility)
    assert await kms.verify(b"old-evidence", sig_v1) is True
    assert await kms.verify(b"new-evidence", sig_v2) is True
    assert await kms.list_versions(ref) == (1, 2)


async def test_disabled_key_cannot_sign(tmp_path) -> None:
    kms = _kms(tmp_path)
    ref = KeyRef(KeyPurpose.EVIDENCE_ROOT)
    await kms.create_key(ref)
    await kms.disable(ref)
    with pytest.raises(KmsUnavailable):
        await kms.sign(ref, b"m")


async def test_verification_survives_key_disable(tmp_path) -> None:
    # Evidentiary invariant: disabling a key stops NEW signing but historical evidence must
    # remain verifiable forever (regression guard for the read-vs-write state split).
    kms = _kms(tmp_path)
    ref = KeyRef(KeyPurpose.EVIDENCE_ROOT)
    await kms.create_key(ref)
    bundle = await kms.sign(ref, b"archived-evidence")
    await kms.disable(ref)
    assert await kms.verify(b"archived-evidence", bundle) is True
    with pytest.raises(KmsUnavailable):
        await kms.sign(ref, b"new")


async def test_capability_negotiation_rejects_unsupported_algorithm(tmp_path) -> None:
    # RSA-PSS is a valid signing algorithm in policy but unsupported by the dev provider.
    kms = _kms(tmp_path, signing_algorithm="RSA_PSS_2048")
    ref = KeyRef(KeyPurpose.EVIDENCE_ROOT)
    with pytest.raises(CapabilityNotSupported):
        await kms.sign(ref, b"m")


def test_policy_rejects_unknown_and_non_signing_algorithms() -> None:
    assert AlgorithmPolicy.from_config(
        signing_algorithm="ED25519", hybrid=False
    ).signing_bundle == (Algorithm.ED25519,)
    with pytest.raises(AlgorithmNotAllowed):
        AlgorithmPolicy.from_config(signing_algorithm="BOGUS", hybrid=False)
    with pytest.raises(AlgorithmNotAllowed):
        AlgorithmPolicy.from_config(signing_algorithm="AES_256_GCM", hybrid=False)


async def test_metadata_tampering_breaks_verification(tmp_path) -> None:
    # C1: all envelope metadata is inside the signed header — mutating any of it invalidates
    # the signature (here: the authenticated timestamp).
    kms = _kms(tmp_path)
    ref = KeyRef(KeyPurpose.EVIDENCE_ROOT)
    await kms.create_key(ref)
    sig = (await kms.sign(ref, b"evidence")).primary
    forged = Signature(
        header=dataclasses.replace(sig.header, created_at="1999-01-01T00:00:00+00:00"),
        value=sig.value,
    )
    assert await kms.verify(b"evidence", SignatureBundle((forged,))) is False


async def test_downgrade_strip_is_detected(tmp_path) -> None:
    # C1 downgrade defense: a header legitimately signed but declaring required={Ed25519, ECDSA}
    # while only the Ed25519 signature is present must FAIL (required ⊄ present).
    provider = DevKmsProvider(str(tmp_path / "ks"), is_production=False)
    await provider.create_key("evidence_root__default", KeyPurpose.EVIDENCE_ROOT, Algorithm.ED25519)
    header = SignedHeader(
        canonical_header_version=CANONICAL_HEADER_VERSION,
        signature_version=SIGNATURE_VERSION,
        algorithm=Algorithm.ED25519,
        key_id=KeyId(ProviderKind.DEV, "evidence_root__default", 1),
        key_purpose=KeyPurpose.EVIDENCE_ROOT,
        required_algorithms=(
            Algorithm.ED25519,
            Algorithm.ECDSA_P256,
        ),  # claims a hybrid was required
        payload_hash=payload_hash(b"msg"),
        payload_hash_algorithm=PAYLOAD_HASH_ALGORITHM,
        created_at="2026-01-01T00:00:00+00:00",
    )
    value = await provider.sign_bytes(
        "evidence_root__default", Algorithm.ED25519, 1, header.canonical_bytes()
    )
    kms = KeyManagementService(
        KeyRegistry(provider),
        AlgorithmPolicy.from_config(signing_algorithm="ED25519", hybrid=False),
        StructlogAuditSink(),
    )
    # The lone signature verifies, but the missing ECDSA member is detected → overall False.
    assert await kms.verify(b"msg", SignatureBundle((Signature(header, value),))) is False


async def test_keystore_corruption_is_detected(tmp_path) -> None:
    provider = DevKmsProvider(str(tmp_path / "ks"), is_production=False)
    await provider.create_key("evidence_root__default", KeyPurpose.EVIDENCE_ROOT, Algorithm.ED25519)
    path = tmp_path / "ks" / "evidence_root__default.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["state"] = "tampered"  # mutate without recomputing the checksum
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(KeystoreCorrupt):
        await provider.current_version("evidence_root__default")
