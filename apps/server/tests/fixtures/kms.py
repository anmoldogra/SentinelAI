"""A real KMS for tests — ADR-0003 §1, ADR-0009.

Every ledger write signs (``record_audit_event`` and ``EvidenceService._append_custody``), so
every test that exercises those paths needs a key management service. This provides one, and it is
deliberately **the real dev provider doing real Ed25519**, not a stub:

* A stub that returned fixed bytes would make the tamper tests vacuous. The whole claim of this
  increment is that a forged entry fails signature verification, and only real asymmetric crypto
  can demonstrate that.
* The dev provider is production code (`platform/crypto/backends/dev.py`) with its own refusal to
  initialize under ``APP_ENV=production``. Testing against it exercises the same envelope,
  registry, and policy path the Vault provider uses; only the key material lives elsewhere.

The keystore is a throwaway directory removed at interpreter exit, so a test run never touches the
developer's ``.kms-dev-keystore`` and two runs cannot collide.

The signing key is created once, at import time, before any event loop is running — hence
``asyncio.run`` here rather than an async fixture. Making this a fixture would mean adding a
parameter to some fifty test signatures for a value none of them care about; a module-level
accessor keeps the noise at the construction site, where the dependency actually is.
"""

from __future__ import annotations

import asyncio
import atexit
import shutil
import tempfile

from sentinelai.platform.auth.repository import MFA_SECRET_KEY
from sentinelai.platform.crypto.audit import StructlogAuditSink
from sentinelai.platform.crypto.backends.dev import DevKmsProvider
from sentinelai.platform.crypto.kms import KeyManagementService
from sentinelai.platform.crypto.ledger import EVIDENCE_LEDGER_KEY
from sentinelai.platform.crypto.policy import AlgorithmPolicy
from sentinelai.platform.crypto.registry import KeyRegistry
from sentinelai.platform.events.signing import EVENT_SIGNING_KEY

_keystore = tempfile.mkdtemp(prefix="sentinelai-test-kms-")
atexit.register(shutil.rmtree, _keystore, True)


def _build(keystore: str) -> KeyManagementService:
    kms = KeyManagementService(
        KeyRegistry(DevKmsProvider(keystore, is_production=False)),
        AlgorithmPolicy.from_config(signing_algorithm="ED25519", hybrid=False),
        StructlogAuditSink(),
    )
    # `create_key` is not idempotent — on an existing key it mints a new version — but this
    # keystore is fresh, so exactly one version exists for the whole run. That matters: a test
    # asserting a stored `key_id` would otherwise see a version that drifts.
    #
    # All three functional roots are created: EVIDENCE_ROOT for the ledgers and anchors
    # (ADR-0003), EVENT_ROOT for outbox signing (ADR-0007), and SESSION_ROOT for the encrypted
    # TOTP secret (ADR-0010, Wave 3.1). They are separate keys by design — an event signature must
    # never be presentable as a custody attestation — so a test KMS holding only some of them
    # would make part of the suite fail on a missing key rather than on anything it asserts.
    asyncio.run(kms.create_key(EVIDENCE_LEDGER_KEY))
    asyncio.run(kms.create_key(EVENT_SIGNING_KEY))
    asyncio.run(kms.create_key(MFA_SECRET_KEY))
    return kms


_KMS = _build(_keystore)


def kms_for_tests() -> KeyManagementService:
    """The shared test KMS. One instance per run, with one version of each functional root."""
    return _KMS


# A SECOND provider on its own keystore: real Ed25519, different key material. Built at import time
# for the same reason `_KMS` is — `create_key` runs through `asyncio.run`, which cannot be called
# from inside a running event loop, and every test that wants this is async.
_alt_keystore = tempfile.mkdtemp(prefix="sentinelai-test-kms-alt-")
atexit.register(shutil.rmtree, _alt_keystore, True)
_ALT_KMS = _build(_alt_keystore)


def foreign_kms_for_tests() -> KeyManagementService:
    """A KMS holding *different* keys from :func:`kms_for_tests`.

    For the tests that must distinguish "signed by us" from "signed by someone else". Verifying a
    genuine, correctly-formed signature against a foreign key is the only way to show the trust
    decision is real rather than a shape check on the envelope.
    """
    return _ALT_KMS
