"""Dev KMS provider — ADR-0009 (revised: H4 atomic keystore + raw signing surface).

REAL software cryptography (pyca/cryptography): Ed25519 / ECDSA-P256 signing, AES-256-GCM AEAD.
Keys live in a local, gitignored keystore; the provider **refuses to initialize when
``APP_ENV=production``** (fail closed). Keystore writes are crash-safe (temp file → fsync →
atomic ``os.replace``) and integrity-checksummed (corruption detection); per-key async locks
prevent read-modify-write races within the process.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from sentinelai.platform.crypto.exceptions import (
    CapabilityNotSupported,
    InsecureConfiguration,
    KeyNotFound,
    KeystoreCorrupt,
    KmsUnavailable,
)
from sentinelai.platform.crypto.types import (
    Algorithm,
    Ciphertext,
    DataKey,
    HealthState,
    HealthStatus,
    KeyId,
    KeyMetadata,
    KeyPurpose,
    ProviderCapabilities,
    ProviderKind,
)

_SAFE = re.compile(r"[^A-Za-z0-9_.-]")


class DevKmsProvider:
    kind = ProviderKind.DEV

    def __init__(self, keystore_path: str, *, is_production: bool) -> None:
        if is_production:  # fail closed — never dev keys in production
            raise InsecureConfiguration("dev KMS provider is forbidden when APP_ENV=production")
        self._root = Path(keystore_path)
        self._root.mkdir(parents=True, exist_ok=True)
        self._locks: dict[str, asyncio.Lock] = {}

    async def start(self) -> None:
        return None

    def _lock(self, key: str) -> asyncio.Lock:
        return self._locks.setdefault(key, asyncio.Lock())

    # -- capabilities / health ---------------------------------------------
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            kind=self.kind,
            signing=True,
            encryption=True,
            data_keys=True,
            rotation=True,
            attestation=False,
            export_public_key=True,
            algorithms=frozenset({Algorithm.ED25519, Algorithm.ECDSA_P256, Algorithm.AES_256_GCM}),
        )

    async def health(self) -> HealthStatus:
        try:
            probe = self._root / ".health"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink(missing_ok=True)
            return HealthStatus(HealthState.READY, self.kind)
        except OSError as exc:
            return HealthStatus(HealthState.UNAVAILABLE, self.kind, detail=str(exc))

    # -- crash-safe, checksummed manifest persistence ----------------------
    def _path(self, key: str) -> Path:
        return self._root / f"{_SAFE.sub('_', key)}.json"

    @staticmethod
    def _checksum(manifest: dict[str, Any]) -> str:
        body = {k: v for k, v in manifest.items() if k != "_checksum"}
        return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()

    def _load(self, key: str) -> dict[str, Any]:
        path = self._path(key)
        if not path.exists():
            raise KeyNotFound(f"key '{key}' not found")
        manifest: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        expected = manifest.get("_checksum")
        if expected is None or expected != self._checksum(manifest):
            raise KeystoreCorrupt(f"keystore manifest for '{key}' failed integrity check")
        return manifest

    def _save(self, key: str, manifest: dict[str, Any]) -> None:
        manifest["_checksum"] = self._checksum(manifest)
        path = self._path(key)
        tmp = path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(json.dumps(manifest))
            f.flush()
            os.fsync(f.fileno())  # durability before the rename
        os.replace(tmp, path)  # atomic on POSIX and Windows — no half-written manifest survives

    def _material(self, algorithm: Algorithm) -> dict[str, str | None]:
        priv: ed25519.Ed25519PrivateKey | ec.EllipticCurvePrivateKey
        if algorithm is Algorithm.ED25519:
            priv = ed25519.Ed25519PrivateKey.generate()
            pub = priv.public_key().public_bytes(
                serialization.Encoding.Raw, serialization.PublicFormat.Raw
            )
        elif algorithm is Algorithm.ECDSA_P256:
            priv = ec.generate_private_key(ec.SECP256R1())
            pub = priv.public_key().public_bytes(
                serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
            )
        elif algorithm is Algorithm.AES_256_GCM:
            return {"secret": base64.b64encode(AESGCM.generate_key(256)).decode(), "public": None}
        else:  # pragma: no cover - guarded by capability check
            raise CapabilityNotSupported(f"dev provider cannot create '{algorithm}'")
        pem = priv.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        return {"private": base64.b64encode(pem).decode(), "public": base64.b64encode(pub).decode()}

    # -- lifecycle ----------------------------------------------------------
    async def create_key(self, key: str, purpose: KeyPurpose, algorithm: Algorithm) -> KeyMetadata:
        async with self._lock(key):
            try:
                manifest = self._load(key)
            except KeyNotFound:
                manifest = {
                    "purpose": purpose.value,
                    "algorithm": algorithm.value,
                    "state": "enabled",
                    "current_version": 0,
                    "versions": {},
                }
            version = manifest["current_version"] + 1
            manifest["versions"][str(version)] = {
                **self._material(Algorithm(manifest["algorithm"])),
                "created_at": datetime.now(UTC).isoformat(),
                "state": "enabled",
            }
            manifest["current_version"] = version
            self._save(key, manifest)
        return await self.get_metadata(key)

    async def rotate(self, key: str) -> KeyId:
        manifest = self._load(key)
        await self.create_key(
            key, KeyPurpose(manifest["purpose"]), Algorithm(manifest["algorithm"])
        )
        return KeyId(self.kind, key, self._load(key)["current_version"])

    async def _set_state(self, key: str, state: str) -> None:
        async with self._lock(key):
            manifest = self._load(key)
            manifest["state"] = state
            self._save(key, manifest)

    async def enable(self, key: str) -> None:
        await self._set_state(key, "enabled")

    async def disable(self, key: str) -> None:
        await self._set_state(key, "disabled")

    async def archive(self, key: str) -> None:
        await self._set_state(key, "archived")

    async def destroy(self, key: str) -> None:
        async with self._lock(key):
            manifest = self._load(key)
            for v in manifest["versions"].values():
                v.pop("private", None)
                v.pop("secret", None)
                v["state"] = "destroyed"
            manifest["state"] = "destroyed"
            self._save(key, manifest)

    async def list_versions(self, key: str) -> tuple[int, ...]:
        return tuple(sorted(int(v) for v in self._load(key)["versions"]))

    async def get_metadata(self, key: str) -> KeyMetadata:
        m = self._load(key)
        return KeyMetadata(
            key_id=KeyId(self.kind, key, m["current_version"]),
            purpose=KeyPurpose(m["purpose"]),
            algorithm=Algorithm(m["algorithm"]),
            state=m["state"],
            created_at=datetime.fromisoformat(
                m["versions"][str(m["current_version"])]["created_at"]
            ),
            versions=tuple(sorted(int(v) for v in m["versions"])),
        )

    async def current_version(self, key: str) -> int:
        return int(self._load(key)["current_version"])

    # -- read / write guards ------------------------------------------------
    def _read_version(self, key: str, version: int | None = None) -> dict[str, Any]:
        m = self._load(key)
        v = version or m["current_version"]
        entry: dict[str, Any] | None = m["versions"].get(str(v))
        if entry is None:
            raise KeyNotFound(f"key '{key}' has no version {v}")
        return entry

    def _assert_writable(self, key: str) -> None:
        state = self._load(key)["state"]
        if state in {"disabled", "archived", "destroyed"}:
            raise KmsUnavailable(f"key '{key}' is {state} and cannot be used for new operations")

    def _load_private(self, entry: dict[str, Any]) -> Any:
        return serialization.load_pem_private_key(base64.b64decode(entry["private"]), password=None)

    # -- signing (raw bytes) -----------------------------------------------
    async def sign_bytes(self, key: str, algorithm: Algorithm, version: int, data: bytes) -> bytes:
        self._assert_writable(key)
        priv = self._load_private(self._read_version(key, version))
        if algorithm is Algorithm.ED25519:
            return cast(bytes, priv.sign(data))
        if algorithm is Algorithm.ECDSA_P256:
            return cast(bytes, priv.sign(data, ec.ECDSA(hashes.SHA256())))
        raise CapabilityNotSupported(f"dev provider cannot sign with '{algorithm}'")

    async def verify_bytes(
        self, data: bytes, signature: bytes, algorithm: Algorithm, key_id: KeyId
    ) -> bool:
        try:
            entry = self._read_version(
                key_id.backend_ref, key_id.version
            )  # reads succeed post-disable
            pub_raw = base64.b64decode(entry["public"])
            if algorithm is Algorithm.ED25519:
                ed25519.Ed25519PublicKey.from_public_bytes(pub_raw).verify(signature, data)
            elif algorithm is Algorithm.ECDSA_P256:
                pub = serialization.load_der_public_key(pub_raw)
                if not isinstance(pub, ec.EllipticCurvePublicKey):
                    return False
                pub.verify(signature, data, ec.ECDSA(hashes.SHA256()))
            else:
                return False
            return True
        except (InvalidSignature, KeyNotFound, KeystoreCorrupt, TypeError, ValueError):
            return False

    async def public_key(self, key_id: KeyId) -> bytes:
        entry = self._read_version(key_id.backend_ref, key_id.version)
        if entry.get("public") is None:
            raise CapabilityNotSupported("symmetric key has no exportable public key")
        return base64.b64decode(entry["public"])

    # -- encryption / data keys --------------------------------------------
    async def encrypt(
        self, key: str, algorithm: Algorithm, plaintext: bytes, aad: bytes | None
    ) -> Ciphertext:
        if algorithm is not Algorithm.AES_256_GCM:
            raise CapabilityNotSupported(f"dev provider cannot encrypt with '{algorithm}'")
        self._assert_writable(key)
        version = self._load(key)["current_version"]
        secret = base64.b64decode(self._read_version(key, version)["secret"])
        nonce = os.urandom(12)  # 96-bit; unique per encryption
        value = AESGCM(secret).encrypt(nonce, plaintext, aad)
        return Ciphertext(
            value=value, nonce=nonce, algorithm=algorithm, key_id=KeyId(self.kind, key, version)
        )

    async def decrypt(self, ciphertext: Ciphertext, aad: bytes | None) -> bytes:
        entry = self._read_version(ciphertext.key_id.backend_ref, ciphertext.key_id.version)
        if entry.get("secret") is None:
            raise KmsUnavailable("key material has been destroyed; cannot decrypt")
        return AESGCM(base64.b64decode(entry["secret"])).decrypt(
            ciphertext.nonce, ciphertext.value, aad
        )

    async def generate_data_key(self, key: str) -> DataKey:
        self._assert_writable(key)
        version = self._load(key)["current_version"]
        kek = base64.b64decode(self._read_version(key, version)["secret"])
        dek = AESGCM.generate_key(256)
        nonce = os.urandom(12)
        wrapped = nonce + AESGCM(kek).encrypt(nonce, dek, None)
        return DataKey(plaintext=dek, wrapped=wrapped, key_id=KeyId(self.kind, key, version))
