"""Malware-scanning port — security-architecture §25, ADR-0008 §2.

A capability port, not an engine. Callers depend on ``MalwareScanner``; the concrete engine
(ClamAV daemon, YARA rules, a commercial appliance) is selected by configuration and is a later
increment. The port takes an **async byte stream** so a multi-GB forensic image is scanned without
being buffered — the same streaming contract as ``platform.security.digest``.

The port reports a verdict; it does **not** decide what happens next. §25's category-aware policy
(a forensic disk image legitimately containing malware is evidence, not a failure) is a domain
rule and lives in the ingestion service.

Signature-freshness monitoring and the air-gapped offline signature import (§25, §41) belong to a
real engine adapter and are not modelled here.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol

from sentinelai.platform.config import Settings
from sentinelai.platform.config import settings as default_settings
from sentinelai.platform.crypto.types import HealthState


class ScannerNotAvailable(Exception):
    """The configured scanner engine has no adapter — fail closed, never scan-as-clean."""


@dataclass(frozen=True, slots=True)
class ScanResult:
    """A scan verdict. ``signature`` names the detection when ``is_clean`` is ``False``."""

    is_clean: bool
    engine: str
    signature: str | None = None


@dataclass(frozen=True, slots=True)
class ScannerHealth:
    """Engine reachability + signature freshness (§25).

    ``DEGRADED`` means the engine answers but its signature database is older than the configured
    maximum age — §25's "a scanner running stale signatures gives false confidence". Reuses
    ``platform.crypto``'s ``HealthState`` rather than defining a parallel enum.
    """

    state: HealthState
    engine: str
    signature_version: str | None = None
    signature_age_hours: float | None = None
    detail: str | None = None


class MalwareScanner(Protocol):
    """Port: scan a byte stream and return a verdict. Never mutates or stores the payload."""

    async def scan(self, stream: AsyncIterator[bytes]) -> ScanResult:
        """Consume ``stream`` and return the verdict. Raises on engine failure — a scan that
        could not run must never be reported as clean."""
        ...

    async def health(self) -> ScannerHealth:
        """Report engine reachability and signature freshness. Never raises."""
        ...


class DummyMalwareScanner:
    """Development/test scanner. **Never permitted in a production-grade profile.**

    Consumes the stream (so streaming behaviour is exercised realistically) and returns a
    configurable verdict. ``settings.validate_for_profile()`` refuses to start a production
    process configured with this scanner — a no-op scanner in production would silently satisfy
    §25's gate while scanning nothing.
    """

    engine = "dummy"

    def __init__(self, *, is_clean: bool = True, signature: str | None = None) -> None:
        self._is_clean = is_clean
        self._signature = signature if not is_clean else None
        self.bytes_scanned = 0

    async def scan(self, stream: AsyncIterator[bytes]) -> ScanResult:
        async for chunk in stream:
            self.bytes_scanned += len(chunk)
        return ScanResult(is_clean=self._is_clean, engine=self.engine, signature=self._signature)

    async def health(self) -> ScannerHealth:
        """Always READY — but it is never permitted in a production-grade profile."""
        return ScannerHealth(
            state=HealthState.READY, engine=self.engine, detail="dummy scanner (non-production)"
        )


def build_malware_scanner(cfg: Settings | None = None) -> MalwareScanner:
    """Construct the configured scanner. Mirrors ``platform.storage.build_object_storage``.

    Production safety is enforced at startup by ``Settings.validate_for_profile`` (which refuses
    the dummy engine), not here.
    """
    cfg = cfg or default_settings
    if cfg.malware_scanner_provider == "dummy":
        return DummyMalwareScanner()
    if cfg.malware_scanner_provider == "clamav":
        from sentinelai.platform.security.clamav import ClamAVMalwareScanner

        return ClamAVMalwareScanner(
            host=cfg.clamav_host,
            port=cfg.clamav_port,
            socket_path=cfg.clamav_socket_path,
            timeout_seconds=cfg.clamav_timeout_seconds,
            max_signature_age_hours=cfg.clamav_max_signature_age_hours,
        )
    raise ScannerNotAvailable(
        f"no adapter for MALWARE_SCANNER_PROVIDER='{cfg.malware_scanner_provider}'"
    )


__all__ = [
    "DummyMalwareScanner",
    "MalwareScanner",
    "ScanResult",
    "ScannerHealth",
    "ScannerNotAvailable",
    "build_malware_scanner",
]
