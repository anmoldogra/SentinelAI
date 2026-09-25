"""The TSA HTTP transport — ADR-0003 §3, Wave 1.3c.

Driven through ``httpx.MockTransport`` rather than a live socket. That is not a shortcut: reaching a
real TSA from a test suite would be slow, flaky, and a genuine egress path in a build whose whole
point is that air-gapped operation works. The mock exercises the real ``HttpTimestampAuthority``
code — the RFC 3161 media types, the status handling, the error translation — against tokens minted
by the real in-process TSA.

The property that matters most here: this client **verifies before returning**. A client that
handed back an unverified token would put bytes from an untrusted server straight into an
evidentiary record and leave every later reader to remember the check.
"""

from __future__ import annotations

import httpx
import pytest

from sentinelai.platform.crypto.tsa import (
    CONTENT_TYPE_QUERY,
    CONTENT_TYPE_REPLY,
    HttpTimestampAuthority,
    TsaError,
    TsaVerificationError,
    build_timestamp_authority,
)
from tests.fixtures.fake_tsa import FakeTsa

_URL = "https://tsa.example/tsr"
_ROOT = b"9f3ac21b" * 8


def _authority(
    tsa: FakeTsa, handler: object, *, trust_anchors: object | None = None
) -> HttpTimestampAuthority:
    """An authority whose transport is the given handler, patched onto httpx."""
    authority = HttpTimestampAuthority(
        _URL,
        tsa.trust_anchors if trust_anchors is None else trust_anchors,  # type: ignore[arg-type]
        timeout_seconds=1.0,
    )
    _install_transport(httpx.MockTransport(handler))  # type: ignore[arg-type]
    return authority


def _install_transport(transport: httpx.MockTransport) -> None:
    """Route every AsyncClient this process builds through ``transport``.

    The authority constructs its own client internally (deliberately — callers should not have to
    manage one), so injecting the transport means patching the constructor. The autouse fixture
    restores it, including when a test fails midway.
    """
    original = httpx.AsyncClient.__init__

    def patched(self: httpx.AsyncClient, **kwargs: object) -> None:
        kwargs["transport"] = transport
        original(self, **kwargs)  # type: ignore[arg-type]

    httpx.AsyncClient.__init__ = patched  # type: ignore[method-assign]


@pytest.fixture
def tsa() -> FakeTsa:
    return FakeTsa()


@pytest.fixture(autouse=True)
def _restore_httpx() -> object:
    """Always put httpx back, even when a test fails partway through."""
    original = httpx.AsyncClient.__init__
    yield None
    httpx.AsyncClient.__init__ = original  # type: ignore[method-assign]


async def test_a_successful_round_trip_returns_a_verified_token(tsa: FakeTsa) -> None:
    """The happy path, and a check that the RFC 3161 media types are actually sent."""
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["content_type"] = request.headers.get("content-type")
        seen["accept"] = request.headers.get("accept")
        seen["body"] = request.content
        return httpx.Response(200, content=tsa.timestamp(request.content))

    authority = _authority(tsa, handler)
    token, verified = await authority.timestamp(_ROOT)

    assert seen["content_type"] == CONTENT_TYPE_QUERY
    assert seen["accept"] == CONTENT_TYPE_REPLY
    assert isinstance(seen["body"], bytes) and len(seen["body"]) > 0
    assert token
    assert verified.hash_algo == "sha256"
    assert "SentinelAI Test TSA" in verified.signer_subject


async def test_a_non_200_response_is_an_error_not_a_missing_timestamp(tsa: FakeTsa) -> None:
    """An HTTP failure is a transport problem, and must be reported as one."""
    authority = _authority(tsa, lambda request: httpx.Response(503, content=b""))

    with pytest.raises(TsaError, match="HTTP 503"):
        await authority.timestamp(_ROOT)


async def test_an_unreachable_tsa_raises_a_tsa_error(tsa: FakeTsa) -> None:
    """A connection failure must surface as ``TsaError`` so the cut can degrade, not crash."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    authority = _authority(tsa, handler)

    with pytest.raises(TsaError, match="could not be reached"):
        await authority.timestamp(_ROOT)


async def test_a_hostile_server_returning_junk_is_rejected(tsa: FakeTsa) -> None:
    """HTTP 200 with garbage is the exact reason this client verifies before returning."""
    authority = _authority(tsa, lambda request: httpx.Response(200, content=b"\x30\x82junk"))

    with pytest.raises(TsaError):
        await authority.timestamp(_ROOT)


async def test_a_server_timestamping_a_different_digest_is_rejected(tsa: FakeTsa) -> None:
    """A TSA (or a proxy) that answers with a token over other bytes must not be trusted.

    This is the substitution attack the verification step exists to catch, driven through the real
    transport rather than by calling the verifier directly.
    """
    liar = FakeTsa(override_digest=b"\x11" * 32)
    authority = _authority(
        liar, lambda request: httpx.Response(200, content=liar.timestamp(request.content))
    )

    with pytest.raises(TsaVerificationError, match="different digest"):
        await authority.timestamp(_ROOT)


async def test_a_replayed_token_is_rejected_by_the_nonce_check(tsa: FakeTsa) -> None:
    """The nonce is checked here, while the client still holds it.

    It is not persisted with the anchor, so this is the only moment replay protection can apply.
    """
    replayer = FakeTsa(override_nonce=999)
    authority = _authority(
        replayer, lambda request: httpx.Response(200, content=replayer.timestamp(request.content))
    )

    with pytest.raises(TsaVerificationError, match="nonce mismatch"):
        await authority.timestamp(_ROOT)


async def test_a_rejecting_tsa_is_reported_with_its_reason(tsa: FakeTsa) -> None:
    refuser = FakeTsa(status="rejection")
    authority = _authority(
        refuser, lambda request: httpx.Response(200, content=refuser.timestamp(request.content))
    )

    with pytest.raises(TsaError, match="refused"):
        await authority.timestamp(_ROOT)


async def test_the_factory_builds_a_working_authority_from_configuration(tsa: FakeTsa) -> None:
    """What the anchor cutter actually calls, wired from settings-shaped values."""
    authority = build_timestamp_authority(
        enabled=True,
        url=_URL,
        trust_anchors_pem=tsa.trust_anchor_pem,
        hash_algo="sha384",
        timeout_seconds=2.0,
    )
    assert isinstance(authority, HttpTimestampAuthority)

    _install_transport(
        httpx.MockTransport(
            lambda request: httpx.Response(200, content=tsa.timestamp(request.content))
        )
    )
    _token, verified = await authority.timestamp(_ROOT)

    # The configured digest algorithm is the one that reaches the wire.
    assert verified.hash_algo == "sha384"
