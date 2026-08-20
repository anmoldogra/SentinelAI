"""Unit tests for the ClamAV adapter (security-architecture §25).

Runs the adapter against a real in-process asyncio TCP server speaking clamd's wire protocol, so
the framing, early-abort handling, and reply translation are exercised end to end — no mocking of
the transport, no ClamAV installation required.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest

from sentinelai.platform.crypto.types import HealthState
from sentinelai.platform.security.clamav import ClamAVMalwareScanner
from sentinelai.platform.security.scanner import ScannerNotAvailable, build_malware_scanner


def _clamav_date(when: datetime) -> str:
    return when.strftime("%a %b %d %H:%M:%S %Y")


class _FakeClamd:
    """Minimal clamd stand-in: reads INSTREAM frames, then replies with a configured verdict."""

    def __init__(
        self,
        *,
        reply: str = "stream: OK",
        version: str | None = None,
        abort_after_bytes: int | None = None,
    ) -> None:
        self._reply = reply
        # `is None`, not `or`: an empty VERSION reply is a case under test, not "unset".
        self._version = (
            version
            if version is not None
            else f"ClamAV 1.0.1/27000/{_clamav_date(datetime.now(UTC))}"
        )
        self._abort_after = abort_after_bytes
        self.received = bytearray()
        self.command: bytes | None = None
        self._server: asyncio.AbstractServer | None = None
        self.port = 0

    async def __aenter__(self) -> _FakeClamd:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc: object) -> None:
        assert self._server is not None
        self._server.close()
        await self._server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            self.command = await reader.readuntil(b"\0")
            if self.command == b"zVERSION\0":
                writer.write(self._version.encode() + b"\0")
                await writer.drain()
                return
            while True:
                header = await reader.readexactly(4)
                size = int.from_bytes(header, "big")
                if size == 0:
                    break
                self.received.extend(await reader.readexactly(size))
                if self._abort_after is not None and len(self.received) >= self._abort_after:
                    break  # clamd aborts as soon as it decides — mid-upload
            writer.write(self._reply.encode() + b"\0")
            await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionResetError):  # pragma: no cover
            pass
        finally:
            writer.close()


def _scanner(port: int, **overrides: object) -> ClamAVMalwareScanner:
    kwargs: dict[str, object] = {
        "host": "127.0.0.1",
        "port": port,
        "timeout_seconds": 5.0,
        "max_signature_age_hours": 48,
    }
    kwargs.update(overrides)
    return ClamAVMalwareScanner(**kwargs)  # type: ignore[arg-type]


async def _stream(*chunks: bytes) -> AsyncIterator[bytes]:
    for chunk in chunks:
        yield chunk


# --- reply translation ------------------------------------------------------


async def test_ok_reply_becomes_a_clean_verdict() -> None:
    async with _FakeClamd(reply="stream: OK") as clamd:
        result = await _scanner(clamd.port).scan(_stream(b"harmless"))
    assert result.is_clean is True
    assert result.engine == "clamav"
    assert result.signature is None


async def test_found_reply_becomes_a_detection_with_its_signature_name() -> None:
    async with _FakeClamd(reply="stream: Eicar-Test-Signature FOUND") as clamd:
        result = await _scanner(clamd.port).scan(_stream(b"malicious"))
    assert result.is_clean is False
    assert result.signature == "Eicar-Test-Signature"


async def test_error_reply_raises_rather_than_passing_as_clean() -> None:
    """A scan that could not run must never be reported clean."""
    async with _FakeClamd(reply="INSTREAM size limit exceeded. ERROR") as clamd:
        with pytest.raises(ScannerNotAvailable, match="error reply"):
            await _scanner(clamd.port).scan(_stream(b"x" * 100))


async def test_empty_reply_raises() -> None:
    async with _FakeClamd(reply="") as clamd:
        with pytest.raises(ScannerNotAvailable):
            await _scanner(clamd.port).scan(_stream(b"x"))


# --- streaming behaviour ----------------------------------------------------


async def test_the_whole_payload_reaches_the_daemon_intact() -> None:
    payload = b"".join(bytes([i % 251]) * 997 for i in range(40))  # ~40 KB, non-uniform
    async with _FakeClamd() as clamd:
        await _scanner(clamd.port).scan(_stream(payload[:1000], payload[1000:]))
    assert bytes(clamd.received) == payload


async def test_chunks_larger_than_the_frame_size_are_split() -> None:
    payload = b"z" * (64 * 1024 * 3 + 17)  # spans several 64 KiB frames
    async with _FakeClamd() as clamd:
        result = await _scanner(clamd.port).scan(_stream(payload))
    assert bytes(clamd.received) == payload
    assert result.is_clean is True


async def test_empty_payload_is_scanned() -> None:
    async with _FakeClamd() as clamd:
        result = await _scanner(clamd.port).scan(_stream())
    assert result.is_clean is True
    assert bytes(clamd.received) == b""


async def test_daemon_answering_mid_upload_still_yields_the_verdict() -> None:
    """clamd aborts as soon as it matches; the closed socket is not an error."""
    async with _FakeClamd(reply="stream: Trojan.Win32 FOUND", abort_after_bytes=1024) as clamd:
        result = await _scanner(clamd.port).scan(_stream(b"m" * (512 * 1024)))
    assert result.is_clean is False
    assert result.signature == "Trojan.Win32"


# --- connection failures ----------------------------------------------------


async def test_unreachable_daemon_raises_scanner_not_available() -> None:
    scanner = _scanner(1, timeout_seconds=2.0)  # nothing listens on port 1
    with pytest.raises(ScannerNotAvailable, match="unreachable"):
        await scanner.scan(_stream(b"x"))


async def test_unreachable_unix_socket_raises_scanner_not_available() -> None:
    scanner = ClamAVMalwareScanner(socket_path="/nonexistent/clamd.ctl", timeout_seconds=2.0)
    with pytest.raises(ScannerNotAvailable):
        await scanner.scan(_stream(b"x"))


async def test_connection_failure_message_does_not_leak_the_payload() -> None:
    scanner = _scanner(1, timeout_seconds=2.0)
    with pytest.raises(ScannerNotAvailable) as caught:
        await scanner.scan(_stream(b"super-secret-evidence-bytes"))
    assert "super-secret-evidence-bytes" not in str(caught.value)


# --- signature freshness (§25) ---------------------------------------------


async def test_fresh_signatures_report_ready() -> None:
    fresh = _clamav_date(datetime.now(UTC) - timedelta(hours=2))
    async with _FakeClamd(version=f"ClamAV 1.0.1/27000/{fresh}") as clamd:
        health = await _scanner(clamd.port).health()
    assert health.state is HealthState.READY
    assert health.signature_version == "27000"
    assert health.signature_age_hours is not None and health.signature_age_hours < 3


async def test_stale_signatures_report_degraded() -> None:
    """§25: a scanner running stale signatures gives false confidence."""
    stale = _clamav_date(datetime.now(UTC) - timedelta(hours=100))
    async with _FakeClamd(version=f"ClamAV 1.0.1/26000/{stale}") as clamd:
        health = await _scanner(clamd.port, max_signature_age_hours=48).health()
    assert health.state is HealthState.DEGRADED
    assert "old" in (health.detail or "")


async def test_unreachable_daemon_reports_unavailable_without_raising() -> None:
    health = await _scanner(1, timeout_seconds=2.0).health()
    assert health.state is HealthState.UNAVAILABLE
    assert health.engine == "clamav"


@pytest.mark.parametrize(
    "version",
    ["ClamAV 1.0.1", "garbage", "", "ClamAV 1.0.1/27000/not-a-date"],
)
async def test_ungradeable_version_replies_report_degraded_not_ready(version: str) -> None:
    async with _FakeClamd(version=version) as clamd:
        health = await _scanner(clamd.port).health()
    assert health.state is HealthState.DEGRADED


# --- factory wiring ---------------------------------------------------------


def test_factory_builds_the_clamav_adapter() -> None:
    from sentinelai.platform.config import Settings

    scanner = build_malware_scanner(
        Settings(malware_scanner_provider="clamav", clamav_host="clamd.internal", clamav_port=3310)
    )
    assert isinstance(scanner, ClamAVMalwareScanner)


def test_factory_still_rejects_an_unknown_provider() -> None:
    from sentinelai.platform.config import Settings

    with pytest.raises(ScannerNotAvailable):
        build_malware_scanner(Settings(malware_scanner_provider="sophos"))
