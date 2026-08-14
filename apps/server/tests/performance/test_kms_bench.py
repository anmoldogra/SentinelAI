"""KMS performance benchmarks / regression guards (ADR-0009 §10).

Gated behind ``RUN_KMS_BENCH=1`` so they never flake a normal CI run, but assert throughput
floors and record p50/p95/p99 latency when run. Targets are conservative software-provider
floors on commodity hardware; tighten per environment. Sign/verify use Ed25519 (dev provider).
"""

from __future__ import annotations

import os
import statistics
import time

import pytest

from sentinelai.platform.crypto.audit import StructlogAuditSink
from sentinelai.platform.crypto.backends.dev import DevKmsProvider
from sentinelai.platform.crypto.kms import KeyManagementService
from sentinelai.platform.crypto.policy import AlgorithmPolicy
from sentinelai.platform.crypto.registry import KeyRegistry
from sentinelai.platform.crypto.types import KeyPurpose, KeyRef

_RUN = os.getenv("RUN_KMS_BENCH")
_N = 2000
# Conservative floors (ops/sec) — Ed25519 software signing/verifying is far faster in practice.
_SIGN_FLOOR = 500.0
_VERIFY_FLOOR = 500.0


def _kms(tmp_path) -> KeyManagementService:  # type: ignore[no-untyped-def]
    provider = DevKmsProvider(str(tmp_path / "ks"), is_production=False)
    policy = AlgorithmPolicy.from_config(signing_algorithm="ED25519", hybrid=False)
    return KeyManagementService(KeyRegistry(provider), policy, StructlogAuditSink())


def _percentiles(samples: list[float]) -> tuple[float, float, float]:
    s = sorted(samples)
    q = statistics.quantiles(s, n=100)
    return s[len(s) // 2], q[94], q[98]  # p50, p95, p99


async def test_sign_and_verify_throughput(tmp_path) -> None:
    if not _RUN:
        pytest.skip("set RUN_KMS_BENCH=1 to run KMS benchmarks")
    kms = _kms(tmp_path)
    ref = KeyRef(KeyPurpose.EVIDENCE_ROOT)
    await kms.create_key(ref)

    sign_lat: list[float] = []
    t0 = time.perf_counter()
    bundles = []
    for _ in range(_N):
        s = time.perf_counter()
        bundles.append(await kms.sign(ref, b"benchmark-message"))
        sign_lat.append(time.perf_counter() - s)
    sign_rate = _N / (time.perf_counter() - t0)

    verify_lat: list[float] = []
    t0 = time.perf_counter()
    for b in bundles:
        s = time.perf_counter()
        assert await kms.verify(b"benchmark-message", b) is True
        verify_lat.append(time.perf_counter() - s)
    verify_rate = _N / (time.perf_counter() - t0)

    sp = _percentiles(sign_lat)
    vp = _percentiles(verify_lat)
    print(
        f"\nKMS sign  : {sign_rate:.0f}/s  "
        f"p50={sp[0] * 1e3:.3f}ms p95={sp[1] * 1e3:.3f}ms p99={sp[2] * 1e3:.3f}ms"
    )
    print(
        f"KMS verify: {verify_rate:.0f}/s  "
        f"p50={vp[0] * 1e3:.3f}ms p95={vp[1] * 1e3:.3f}ms p99={vp[2] * 1e3:.3f}ms"
    )
    assert sign_rate > _SIGN_FLOOR
    assert verify_rate > _VERIFY_FLOOR
