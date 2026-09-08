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

from sentinelai.platform.crypto.audit import StructlogAuditSink
from sentinelai.platform.crypto.backends.dev import DevKmsProvider
from sentinelai.platform.crypto.kms import KeyManagementService
from sentinelai.platform.crypto.ledger import EVIDENCE_LEDGER_KEY
from sentinelai.platform.crypto.policy import AlgorithmPolicy
from sentinelai.platform.crypto.registry import KeyRegistry

_keystore = tempfile.mkdtemp(prefix="sentinelai-test-kms-")
atexit.register(shutil.rmtree, _keystore, True)


def _build() -> KeyManagementService:
    kms = KeyManagementService(
        KeyRegistry(DevKmsProvider(_keystore, is_production=False)),
        AlgorithmPolicy.from_config(signing_algorithm="ED25519", hybrid=False),
        StructlogAuditSink(),
    )
    # `create_key` is not idempotent — on an existing key it mints a new version — but this
    # keystore is fresh, so exactly one version exists for the whole run. That matters: a test
    # asserting a stored `key_id` would otherwise see a version that drifts.
    asyncio.run(kms.create_key(EVIDENCE_LEDGER_KEY))
    return kms


_KMS = _build()


def kms_for_tests() -> KeyManagementService:
    """The shared test KMS. One instance per run, with one key version."""
    return _KMS
