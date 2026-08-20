"""ClamAV (``clamd``) adapter for the ``MalwareScanner`` port — security-architecture §25.

Speaks clamd's wire protocol directly over ``asyncio`` streams (TCP or a UNIX socket). No client
library is added: the protocol needed here is two commands, and a raw client avoids taking a
dependency — and its transitive supply-chain surface (§42-43) — on an unmaintained wrapper for
~100 lines of framing.

**Streaming.** ``INSTREAM`` sends the payload as length-prefixed chunks, so a multi-GB forensic
image is scanned without ever being buffered — the port's streaming contract end to end. clamd may
answer *before* the upload finishes (it aborts as soon as it matches, or when
``StreamMaxLength`` is exceeded); that closes the socket mid-write, which is handled as a normal
outcome rather than an error, and the verdict is read from the response.

**Freshness (§25).** ``VERSION`` reports the signature database build date; ``health()`` reports
``DEGRADED`` once it is older than the configured maximum, because a scanner running stale
signatures gives false confidence.

Signature *updates* (freshclam, or §41's air-gapped offline import) are a deployment concern and
are deliberately not performed here — this adapter only observes and reports freshness.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import suppress
from datetime import UTC, datetime

from sentinelai.platform.crypto.types import HealthState
from sentinelai.platform.logging import log
from sentinelai.platform.security.scanner import (
    ScannerHealth,
    ScannerNotAvailable,
    ScanResult,
)

_ENGINE = "clamav"
# clamd's "z" command prefix = NUL-terminated command and NUL-terminated reply.
_CMD_INSTREAM = b"zINSTREAM\0"
_CMD_VERSION = b"zVERSION\0"
# INSTREAM frame size. clamd accepts up to StreamMaxLength per stream; 64 KiB frames keep the
# per-frame buffer small while staying far under any sane clamd chunk limit.
_FRAME = 64 * 1024
_END_OF_STREAM = b"\x00\x00\x00\x00"
# clamd's VERSION date format, e.g. "Mon Sep 11 08:30:00 2023".
_VERSION_DATE_FORMAT = "%a %b %d %H:%M:%S %Y"


class ClamAVMalwareScanner:
    """``MalwareScanner`` backed by a clamd daemon over TCP or a UNIX socket."""

    engine = _ENGINE

    def __init__(
        self,
        *,
        host: str = "localhost",
        port: int = 3310,
        socket_path: str | None = None,
        timeout_seconds: float = 300.0,
        max_signature_age_hours: int = 48,
    ) -> None:
        self._host = host
        self._port = port
        self._socket_path = socket_path
        self._timeout = timeout_seconds
        self._max_signature_age = max_signature_age_hours

    # -- transport ----------------------------------------------------------

    async def _connect(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        """Open a clamd connection, or raise ``ScannerNotAvailable``.

        A daemon that cannot be reached is never allowed to degrade into a clean verdict.
        """
        try:
            if self._socket_path:
                # UNIX sockets do not exist on Windows; resolve dynamically so this module stays
                # importable (and type-checkable) on every platform.
                open_unix = getattr(asyncio, "open_unix_connection", None)
                if open_unix is None:
                    raise ScannerNotAvailable(
                        "CLAMAV_SOCKET_PATH is set but UNIX sockets are unsupported on this "
                        "platform — use CLAMAV_HOST/CLAMAV_PORT"
                    )
                return await asyncio.wait_for(open_unix(self._socket_path), timeout=self._timeout)
            return await asyncio.wait_for(
                asyncio.open_connection(self._host, self._port), timeout=self._timeout
            )
        except (TimeoutError, OSError, NotImplementedError) as exc:
            # The endpoint is operational detail, not a secret; the payload is never named here.
            raise ScannerNotAvailable(f"clamd unreachable at {self._endpoint()}") from exc

    def _endpoint(self) -> str:
        return self._socket_path or f"{self._host}:{self._port}"

    @staticmethod
    async def _close(writer: asyncio.StreamWriter) -> None:
        writer.close()
        with suppress(OSError, asyncio.CancelledError):  # best-effort teardown
            await writer.wait_closed()

    async def _read_reply(self, reader: asyncio.StreamReader) -> str:
        try:
            raw = await asyncio.wait_for(reader.readuntil(b"\0"), timeout=self._timeout)
        except asyncio.IncompleteReadError as exc:
            raw = exc.partial  # daemon closed without the NUL terminator
        except (TimeoutError, OSError) as exc:
            raise ScannerNotAvailable(f"clamd did not reply at {self._endpoint()}") from exc
        return raw.rstrip(b"\0").decode("utf-8", errors="replace").strip()

    # -- port -------------------------------------------------------------

    async def scan(self, stream: AsyncIterator[bytes]) -> ScanResult:
        """Stream the payload to clamd via ``INSTREAM`` and translate the reply into a verdict."""
        reader, writer = await self._connect()
        try:
            writer.write(_CMD_INSTREAM)
            await writer.drain()

            aborted_early = False
            try:
                async for chunk in stream:
                    for start in range(0, len(chunk), _FRAME):
                        frame = chunk[start : start + _FRAME]
                        writer.write(len(frame).to_bytes(4, "big") + frame)
                    await writer.drain()
                if not aborted_early:
                    writer.write(_END_OF_STREAM)
                    await writer.drain()
            except (ConnectionResetError, BrokenPipeError):
                # clamd answered before we finished uploading (match found, or size limit hit).
                # Not an error: the verdict is waiting on the socket.
                aborted_early = True

            reply = await self._read_reply(reader)
        finally:
            await self._close(writer)
        return self._verdict(reply)

    @staticmethod
    def _verdict(reply: str) -> ScanResult:
        """Translate a clamd INSTREAM reply into a ``ScanResult``.

        ``stream: OK`` → clean; ``stream: <SIGNATURE> FOUND`` → detection;
        anything else (including ``ERROR``) is a failed scan and must raise, never pass.
        """
        if reply.endswith("OK"):
            return ScanResult(is_clean=True, engine=_ENGINE)
        if reply.endswith("FOUND"):
            body = reply[: -len("FOUND")].strip()
            _, _, signature = body.partition(":")
            return ScanResult(
                is_clean=False, engine=_ENGINE, signature=signature.strip() or "unnamed"
            )
        raise ScannerNotAvailable(f"clamd returned an error reply: {reply or '<empty>'}")

    async def health(self) -> ScannerHealth:
        """Reachability + signature freshness (§25). Never raises."""
        try:
            reader, writer = await self._connect()
            try:
                writer.write(_CMD_VERSION)
                await writer.drain()
                reply = await self._read_reply(reader)
            finally:
                await self._close(writer)
        except ScannerNotAvailable as exc:
            log.warning("clamav_unreachable", endpoint=self._endpoint())
            return ScannerHealth(state=HealthState.UNAVAILABLE, engine=_ENGINE, detail=str(exc))
        except Exception:  # pragma: no cover - health must never raise
            log.warning("clamav_health_failed", endpoint=self._endpoint())
            return ScannerHealth(
                state=HealthState.UNAVAILABLE, engine=_ENGINE, detail="health check failed"
            )
        return self._freshness(reply)

    def _freshness(self, version_reply: str) -> ScannerHealth:
        """Parse ``ClamAV <ver>/<sig-version>/<sig-build-date>`` and grade signature age."""
        parts = version_reply.split("/")
        if len(parts) < 3:
            # A daemon that answers but won't state its signature version can't be graded fresh.
            return ScannerHealth(
                state=HealthState.DEGRADED,
                engine=_ENGINE,
                detail=f"unparseable VERSION reply: {version_reply or '<empty>'}",
            )
        signature_version = parts[1].strip()
        try:
            built = datetime.strptime(parts[2].strip(), _VERSION_DATE_FORMAT).replace(tzinfo=UTC)
        except ValueError:
            return ScannerHealth(
                state=HealthState.DEGRADED,
                engine=_ENGINE,
                signature_version=signature_version,
                detail="unparseable signature build date",
            )
        age_hours = (datetime.now(UTC) - built).total_seconds() / 3600
        stale = age_hours > self._max_signature_age
        if stale:
            log.warning(
                "clamav_signatures_stale",
                age_hours=round(age_hours, 1),
                max_age_hours=self._max_signature_age,
            )
        return ScannerHealth(
            state=HealthState.DEGRADED if stale else HealthState.READY,
            engine=_ENGINE,
            signature_version=signature_version,
            signature_age_hours=round(age_hours, 2),
            detail=(
                f"signature database is {age_hours:.1f}h old (max {self._max_signature_age}h)"
                if stale
                else None
            ),
        )


__all__ = ["ClamAVMalwareScanner"]
