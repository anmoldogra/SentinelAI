"""RFC 3161 trusted timestamping — ADR-0003 §3, modernization Wave 1.3c.

**The one attack WORM does not close.** Wave 1.3 made an anchor *undeletable*: once written under
COMPLIANCE-mode Object Lock, no principal can remove it, so a truncated ledger is detectable. It
says
nothing about *when* the anchor was made. An attacker who controls both the application and the
clock
can doctor history and publish a fresh anchor over it, backdated to look old. They cannot replace an
anchor already in WORM — so this is a narrow residual rather than the hole truncation was — but it
is
the last one, and it is the difference between "this history is intact" and "this history is intact
*and was committed to at a time a third party attests to*".

A timestamp token closes it because the attesting signature is made by someone else, over our
digest,
at a time we do not control.

**Why verification is the hard half.** Obtaining a token is an HTTP POST. An *unverified* token
proves
nothing at all — it is a blob a hostile server, or a hostile proxy, can return arbitrary bytes for.
So
this module's real content is :func:`verify_timestamp_token`, and it checks all of:

1. the response status is granted (a rejection carries no token);
2. the CMS content type is ``signed_data`` wrapping ``tst_info``, with content present (not
detached);
3. the timestamped digest is *our* digest, under the algorithm the token itself names;
4. the nonce matches the one we sent, which is what makes a replayed old token useless;
5. exactly one ``SignerInfo`` — RFC 3161 §2.4.2 permits no more;
6. the ``message-digest`` signed attribute equals the digest of the encapsulated ``TSTInfo``, and
the
   ``content-type`` signed attribute names ``tst_info`` — without these two, signed attributes could
   be lifted from an unrelated token;
7. the signature over the DER-encoded ``SignedAttrs`` verifies under the signer certificate's key;
8. the signer certificate carries the ``id-kp-timeStamping`` EKU as its **only** extended key usage
   and marked critical (RFC 3161 §2.3) — a general-purpose TLS certificate must not be able to
   timestamp;
9. the signer certificate was valid at the claimed ``genTime``;
10. the signer certificate chains to a configured trust anchor.

**What is deliberately NOT checked, and must be stated rather than implied.** There is no revocation
checking — no CRL fetch, no OCSP. Both need network calls that an air-gapped deployment cannot make
and that would turn verification of *archived* evidence into a live-connectivity problem. The
consequence is real: a token signed by a certificate revoked after the fact still verifies here. The
mitigation is operational, not cryptographic — the trust anchor set is deployment-controlled, so a
compromised TSA is removed by configuration, and every anchor also carries the WORM and Merkle
guarantees that do not depend on the TSA at all. Path building is also bounded: the signer
certificate
is verified against the trust anchors directly, or through at most the intermediates the token
itself
carried.

``asn1crypto`` is used **only as a DER codec**. Every signature check and every certificate parse
goes
through ``cryptography``, which is the audited implementation. Hand-rolling ASN.1 parsing for a
security boundary is exactly the "lighter version of a security control"
``security-architecture.md``
warns against, and hand-rolling signature verification would be worse.
"""

from __future__ import annotations

import secrets
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, Protocol

import httpx
from asn1crypto import cms as asn1_cms
from asn1crypto import core as asn1_core
from asn1crypto import tsp as asn1_tsp
from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, padding, rsa
from cryptography.hazmat.primitives.serialization import Encoding

from sentinelai.platform.crypto.exceptions import CryptoError

# RFC 3161 §2.3: the timestamping EKU, which must be the signer certificate's only one.
EKU_TIME_STAMPING: Final = "1.3.6.1.5.5.7.3.8"
# RFC 3161 §2.4.2: the encapsulated content type of a timestamp token.
CONTENT_TYPE_TST_INFO: Final = "tst_info"

# Digest algorithms accepted in a message imprint. SHA-1 is absent deliberately: a timestamp over a
# SHA-1 imprint is a timestamp over a digest an attacker can collide, which makes the attestation
# meaningless even though the token itself verifies.
_HASHES: Final[dict[str, Any]] = {
    "sha256": hashes.SHA256,
    "sha384": hashes.SHA384,
    "sha512": hashes.SHA512,
}

# Nonce width. RFC 3161 allows any integer; 64 bits of CSPRNG output makes a replayed token
# astronomically unlikely to match, which is the property the nonce exists for.
_NONCE_BITS: Final = 64


class _TimeStampResp(asn1_core.Sequence):  # type: ignore[misc]  # asn1crypto is untyped
    """``TimeStampResp`` with ``timeStampToken`` actually optional, per RFC 3161 §2.4.2.

    ``asn1crypto.tsp.TimeStampResp`` declares the token **required**, which is a deviation from the
    RFC: a rejecting TSA legitimately returns status only. Parsing a real rejection through
    asn1crypto's class raises, so the useful diagnostic — *why* the TSA refused — would be reported
    as "malformed response" instead, sending an operator to debug the wrong thing entirely.
    """

    # asn1crypto's schema DSL requires a mutable class-level list; RUF012 does not apply to it.
    _fields = [  # noqa: RUF012
        ("status", asn1_tsp.PKIStatusInfo),
        ("time_stamp_token", asn1_cms.ContentInfo, {"optional": True}),
    ]


class TsaError(CryptoError):
    """A timestamp could not be obtained or parsed.

    Distinct from :class:`TsaVerificationError`: this means the operation could not be completed
    (unreachable TSA, malformed DER, rejected request). Callers may retry, and an anchor cut treats
    it
    as a non-fatal degradation — an untimestamped anchor is still an anchor.
    """


class TsaVerificationError(TsaError):
    """A timestamp token was presented and does not verify.

    Never retried and never tolerated. A token that fails any check in
    :func:`verify_timestamp_token`
    is evidence of a problem, not of a transient fault, and treating it as absent would let a forged
    token be silently downgraded to "no timestamp".
    """


@dataclass(frozen=True, slots=True)
class TimestampRequest:
    """A DER-encoded ``TimeStampReq`` and the state needed to verify its response.

    The nonce and digest are carried alongside the bytes because verification needs both, and
    re-deriving them from the request would mean parsing back what we just encoded.
    """

    der: bytes
    nonce: int
    digest: bytes
    hash_algo: str


@dataclass(frozen=True, slots=True)
class VerifiedTimestamp:
    """What a verified token actually attests to."""

    gen_time: datetime
    serial_number: int
    hash_algo: str
    signer_subject: str
    policy: str | None


def _digest(message: bytes, hash_algo: str) -> bytes:
    if hash_algo not in _HASHES:
        raise TsaError(
            f"unsupported timestamp digest '{hash_algo}'; permitted: {sorted(_HASHES)}. SHA-1 is "
            "excluded on purpose — a timestamp over a collidable digest attests to nothing."
        )
    digest = hashes.Hash(_HASHES[hash_algo]())
    digest.update(message)
    return digest.finalize()


def build_timestamp_request(
    message: bytes, *, hash_algo: str = "sha256", nonce: int | None = None
) -> TimestampRequest:
    """Build a ``TimeStampReq`` over ``message`` — RFC 3161 §2.4.1.

    ``cert_req`` is set so the TSA returns its signing certificate inside the token. Without it a
    verifier needs the certificate out of band, and an anchor document that cannot be verified from
    its own contents defeats the purpose of making the anchor self-describing.

    The nonce is generated here by default rather than taken from the caller, because a caller that
    forgot one would produce a request whose response could be satisfied by any previously-issued
    token for the same digest.
    """
    request_nonce = secrets.randbits(_NONCE_BITS) if nonce is None else nonce
    digest = _digest(message, hash_algo)
    request = asn1_tsp.TimeStampReq(
        {
            "version": "v1",
            "message_imprint": {
                "hash_algorithm": {"algorithm": hash_algo},
                "hashed_message": digest,
            },
            "nonce": request_nonce,
            "cert_req": True,
        }
    )
    return TimestampRequest(
        der=request.dump(), nonce=request_nonce, digest=digest, hash_algo=hash_algo
    )


def token_from_response(response_der: bytes) -> bytes:
    """Extract the timestamp token from a ``TimeStampResp``, or raise.

    A response whose status is not ``granted``/``grantedWithMods`` carries no token, and the failure
    info is the only useful thing in it — so it is surfaced in the error rather than flattened into
    "no timestamp available".
    """
    try:
        response = _TimeStampResp.load(response_der)
        status_info = response["status"]
        status = status_info["status"].native
    except TsaError:
        raise
    except Exception as exc:
        raise TsaError(f"TSA response is not a well-formed TimeStampResp: {exc}") from exc

    if status not in ("granted", "granted_with_mods"):
        fail_info = status_info["fail_info"].native
        reason = fail_info if fail_info else "unspecified"
        raise TsaError(f"TSA refused the request: status={status} fail_info={reason}")

    token = response["time_stamp_token"]
    if isinstance(token, asn1_core.Void) or token.native is None:
        raise TsaError("TSA granted the request but returned no timestamp token")
    return bytes(token.dump())


def _signed_attr(signer_info: Any, name: str) -> Any | None:
    """One signed attribute by name, or ``None``."""
    attrs = signer_info["signed_attrs"]
    if attrs.native is None:
        return None
    for attr in attrs:
        if attr["type"].native == name:
            return attr["values"]
    return None


def _find_signer_certificate(
    signed_data: Any, signer_info: Any
) -> tuple[x509.Certificate, list[x509.Certificate]]:
    """The signer certificate plus the other certificates the token carried.

    Matched on issuer-and-serial (or subject key identifier), never "the first certificate in the
    bag": a token may legitimately carry a whole chain, and picking the wrong one would verify the
    signature against a key that did not make it — or fail against a key that did.
    """
    certificates: list[x509.Certificate] = []
    bag = signed_data["certificates"]
    if bag.native is not None:
        for choice in bag:
            parsed = choice.chosen
            if parsed.__class__.__name__ != "Certificate":
                continue  # attribute certificates and other choices are not signers
            certificates.append(x509.load_der_x509_certificate(parsed.dump()))

    if not certificates:
        raise TsaVerificationError(
            "the timestamp token carries no certificates, so its signature cannot be verified from "
            "the token alone (the request sets cert_req=True precisely to avoid this)"
        )

    sid = signer_info["sid"]
    if sid.name == "issuer_and_serial_number":
        serial = sid.chosen["serial_number"].native
        # Compared as DER rather than as parsed names: `cryptography` exposes no DER-name loader,
        # and
        # comparing rendered strings would make two distinct names with the same text collide.
        issuer_der = bytes(sid.chosen["issuer"].dump())
        for candidate in certificates:
            if candidate.serial_number == serial and candidate.issuer.public_bytes() == issuer_der:
                return candidate, [c for c in certificates if c is not candidate]
    else:  # subject_key_identifier
        wanted = sid.chosen.native
        for candidate in certificates:
            try:
                ski = candidate.extensions.get_extension_for_class(
                    x509.SubjectKeyIdentifier
                ).value.digest
            except x509.ExtensionNotFound:
                continue
            if ski == wanted:
                return candidate, [c for c in certificates if c is not candidate]

    raise TsaVerificationError(
        "the SignerInfo identifies a certificate that the token does not contain"
    )


def _assert_timestamping_certificate(certificate: x509.Certificate, gen_time: datetime) -> None:
    """RFC 3161 §2.3: the EKU must be timestamping, alone, and critical; validity must cover
    genTime."""
    try:
        eku_ext = certificate.extensions.get_extension_for_class(x509.ExtendedKeyUsage)
    except x509.ExtensionNotFound as exc:
        raise TsaVerificationError(
            "the timestamp signer certificate has no extendedKeyUsage extension; RFC 3161 §2.3 "
            "requires id-kp-timeStamping, so this certificate is not entitled to timestamp"
        ) from exc

    oids = {usage.dotted_string for usage in eku_ext.value}
    if oids != {EKU_TIME_STAMPING}:
        raise TsaVerificationError(
            f"the timestamp signer certificate's extendedKeyUsage is {sorted(oids)}; RFC 3161 §2.3 "
            "requires id-kp-timeStamping and nothing else, so a general-purpose certificate cannot "
            "be repurposed to attest to time"
        )
    if not eku_ext.critical:
        raise TsaVerificationError(
            "the timestamp signer certificate's extendedKeyUsage is not marked critical, which "
            "RFC 3161 §2.3 requires"
        )

    not_before = certificate.not_valid_before_utc
    not_after = certificate.not_valid_after_utc
    if not not_before <= gen_time <= not_after:
        raise TsaVerificationError(
            f"the timestamp claims genTime {gen_time.isoformat()}, outside the signer "
            f"certificate's validity ({not_before.isoformat()} .. {not_after.isoformat()})"
        )


def _verify_signature(
    certificate: x509.Certificate, signature: bytes, signed_bytes: bytes, digest_algo: str
) -> None:
    """Verify one signature under whatever key type the certificate carries."""
    if digest_algo not in _HASHES:
        raise TsaVerificationError(f"unsupported signature digest '{digest_algo}'")
    algorithm = _HASHES[digest_algo]()
    public_key = certificate.public_key()
    try:
        if isinstance(public_key, rsa.RSAPublicKey):
            public_key.verify(signature, signed_bytes, padding.PKCS1v15(), algorithm)
        elif isinstance(public_key, ec.EllipticCurvePublicKey):
            public_key.verify(signature, signed_bytes, ec.ECDSA(algorithm))
        elif isinstance(public_key, ed25519.Ed25519PublicKey):
            public_key.verify(signature, signed_bytes)
        else:
            raise TsaVerificationError(
                f"unsupported timestamp signer key type {type(public_key).__name__}"
            )
    except InvalidSignature as exc:
        raise TsaVerificationError("the timestamp token's signature does not verify") from exc


def _verify_chain(
    signer: x509.Certificate,
    intermediates: Sequence[x509.Certificate],
    trust_anchors: Sequence[x509.Certificate],
) -> None:
    """Chain ``signer`` to a trust anchor, directly or through one token-supplied intermediate.

    Deliberately bounded, and the bound is documented rather than hidden: this verifies
    issuer/subject
    linkage and the issuer's signature over each certificate, and nothing else. There is **no
    revocation checking** (see the module docstring) and no name-constraint or policy processing.
    The
    trust anchor set is the deployment's control surface: a TSA that should no longer be trusted is
    removed from configuration.
    """
    if not trust_anchors:
        raise TsaVerificationError(
            "no TSA trust anchors are configured, so a timestamp token cannot be verified. A token "
            "verified against an empty trust store would attest to nothing while appearing to pass."
        )

    def signed_by(child: x509.Certificate, issuer: x509.Certificate) -> bool:
        if child.issuer != issuer.subject:
            return False
        signature_hash = child.signature_hash_algorithm
        if signature_hash is None:  # Ed25519 and friends carry no separate digest
            try:
                issuer.public_key().verify(  # type: ignore[call-arg,union-attr]
                    child.signature, child.tbs_certificate_bytes
                )
                return True
            except Exception:
                return False
        try:
            _verify_signature(
                issuer, child.signature, child.tbs_certificate_bytes, signature_hash.name
            )
            return True
        except TsaVerificationError:
            return False

    for anchor in trust_anchors:
        if signed_by(signer, anchor):
            return
    for intermediate in intermediates:
        if signed_by(signer, intermediate) and any(
            signed_by(intermediate, anchor) for anchor in trust_anchors
        ):
            return

    raise TsaVerificationError(
        f"the timestamp signer '{signer.subject.rfc4514_string()}' does not chain to any "
        "configured TSA trust anchor"
    )


def verify_timestamp_token(
    token_der: bytes,
    *,
    message: bytes,
    trust_anchors: Sequence[x509.Certificate],
    nonce: int | None = None,
) -> VerifiedTimestamp:
    """Verify a timestamp token against ``message`` and return what it attests to.

    Raises :class:`TsaVerificationError` on any failure. There is no partial success and no boolean
    return: a caller holding a ``VerifiedTimestamp`` has a third-party attestation that ``message``
    existed at ``gen_time``, and a caller holding an exception has nothing.

    ``nonce`` should be supplied whenever it is known (i.e. when verifying a response to a request
    we
    just made). It is optional because re-verifying an *archived* token years later is a first-class
    use — the nonce is not stored in the anchor document, and its absence at that point only means
    replay protection was checked once, at issue time, rather than that it was never checked.
    """
    try:
        content_info = asn1_cms.ContentInfo.load(token_der)
        if content_info["content_type"].native != "signed_data":
            raise TsaVerificationError(
                f"a timestamp token must be CMS signed_data, got "
                f"'{content_info['content_type'].native}'"
            )
        signed_data = content_info["content"]
        encap = signed_data["encap_content_info"]
        if encap["content_type"].native != CONTENT_TYPE_TST_INFO:
            raise TsaVerificationError(
                f"the token's encapsulated content type is '{encap['content_type'].native}', "
                f"not '{CONTENT_TYPE_TST_INFO}'"
            )
        # `.contents` is the raw DER inside the OCTET STRING, which is what the message-digest
        # attribute covers. `.native` would auto-parse into a dict here and silently give the wrong
        # bytes to digest — the kind of mistake that produces a verifier which accepts everything.
        tst_bytes = bytes(encap["content"].contents)
        if not tst_bytes:
            raise TsaVerificationError(
                "the token has detached content; a timestamp token must encapsulate its TSTInfo, "
                "because a detached one could be paired with any TSTInfo after the fact"
            )
        tst_info = asn1_tsp.TSTInfo.load(tst_bytes)
    except TsaVerificationError:
        raise
    except Exception as exc:
        raise TsaVerificationError(f"the timestamp token is not well-formed DER: {exc}") from exc

    # --- what was timestamped must be OUR message, under the algorithm the token names ---
    imprint = tst_info["message_imprint"]
    token_hash_algo = imprint["hash_algorithm"]["algorithm"].native
    expected = _digest(message, token_hash_algo)
    if bytes(imprint["hashed_message"].native) != expected:
        raise TsaVerificationError(
            "the timestamp attests to a different digest than the message supplied; this token "
            "does not cover this anchor"
        )

    # --- replay protection ---
    if nonce is not None:
        token_nonce = tst_info["nonce"].native
        if token_nonce != nonce:
            raise TsaVerificationError(
                f"timestamp nonce mismatch (sent {nonce}, token carries {token_nonce}); the "
                "response may be a replay of an earlier token for the same digest"
            )

    signer_infos = signed_data["signer_infos"]
    if len(signer_infos) != 1:
        raise TsaVerificationError(
            f"a timestamp token must carry exactly one SignerInfo (RFC 3161 §2.4.2), got "
            f"{len(signer_infos)}"
        )
    signer_info = signer_infos[0]
    digest_algo = signer_info["digest_algorithm"]["algorithm"].native

    # --- the signed attributes must bind this signature to THIS TSTInfo ---
    signed_attrs = signer_info["signed_attrs"]
    if signed_attrs.native is None:
        raise TsaVerificationError(
            "the token has no signed attributes, so its signature is not bound to the TSTInfo it "
            "accompanies"
        )
    content_type_attr = _signed_attr(signer_info, "content_type")
    if content_type_attr is None or content_type_attr[0].native != CONTENT_TYPE_TST_INFO:
        raise TsaVerificationError(
            "the token's content-type signed attribute is missing or does not name tst_info"
        )
    message_digest_attr = _signed_attr(signer_info, "message_digest")
    if message_digest_attr is None:
        raise TsaVerificationError("the token has no message-digest signed attribute")
    if bytes(message_digest_attr[0].native) != _digest(tst_bytes, digest_algo):
        raise TsaVerificationError(
            "the token's message-digest attribute does not match the encapsulated TSTInfo, so the "
            "signed attributes belong to a different token"
        )

    gen_time = tst_info["gen_time"].native
    if gen_time.tzinfo is None:  # pragma: no cover - asn1crypto always returns aware datetimes
        gen_time = gen_time.replace(tzinfo=UTC)

    certificate, intermediates = _find_signer_certificate(signed_data, signer_info)
    _assert_timestamping_certificate(certificate, gen_time)

    # RFC 5652 §5.4: the signature covers the DER SET OF SignedAttrs, not the implicit [0] tagging
    # it carries inside SignerInfo. `untag()` is what produces the bytes that were actually signed;
    # signing the tagged form would fail against every real TSA.
    _verify_signature(
        certificate,
        bytes(signer_info["signature"].native),
        signed_attrs.untag().dump(),
        digest_algo,
    )
    _verify_chain(certificate, intermediates, trust_anchors)

    return VerifiedTimestamp(
        gen_time=gen_time.astimezone(UTC),
        serial_number=int(tst_info["serial_number"].native),
        hash_algo=token_hash_algo,
        signer_subject=certificate.subject.rfc4514_string(),
        policy=tst_info["policy"].native if tst_info["policy"].native else None,
    )


def load_trust_anchors(pem_bundle: str) -> list[x509.Certificate]:
    """Parse a PEM bundle of TSA trust anchors.

    Returns an empty list for empty configuration rather than raising: a deployment with no TSA
    configured legitimately has no anchors, and :func:`verify_timestamp_token` is the place that
    refuses to verify against an empty store.
    """
    if not pem_bundle.strip():
        return []
    return x509.load_pem_x509_certificates(pem_bundle.encode("utf-8"))


def certificates_to_pem(certificates: Sequence[x509.Certificate]) -> str:
    """Render certificates as a PEM bundle — the inverse of :func:`load_trust_anchors`."""
    return "".join(c.public_bytes(Encoding.PEM).decode("ascii") for c in certificates)


# ---------------------------------------------------------------------------------------
# Fetching — the easy half, kept behind a Protocol so nothing in the anchor path depends on HTTP
# ---------------------------------------------------------------------------------------

# RFC 3161 §3.4 media types for the HTTP POST transport.
CONTENT_TYPE_QUERY: Final = "application/timestamp-query"
CONTENT_TYPE_REPLY: Final = "application/timestamp-reply"


class TimestampAuthority(Protocol):
    """Obtain a verified timestamp over ``message``.

    A Protocol rather than a concrete client for two reasons. The anchor path must be testable
    without a network, and — more importantly — an air-gapped deployment needs a *substitutable*
    implementation rather than a disabled code path full of ``if tsa_enabled`` branches. The absence
    of timestamping is expressed by passing no authority at all.
    """

    async def timestamp(self, message: bytes) -> tuple[bytes, VerifiedTimestamp]:
        """Return the DER token and what it attests to, or raise :class:`TsaError`."""
        ...


class HttpTimestampAuthority:
    """RFC 3161 over HTTP — the only transport this platform speaks to a TSA.

    **Verifies before returning.** The request/response round trip and the verification are one
    operation here on purpose: a client that returned an unverified token would put a blob from an
    untrusted server into an evidentiary record, and every later reader would have to remember to
    check it. Returning only verified tokens makes the unsafe path unreachable.
    """

    def __init__(
        self,
        url: str,
        trust_anchors: Sequence[x509.Certificate],
        *,
        hash_algo: str = "sha256",
        timeout_seconds: float = 10.0,
    ) -> None:
        if not url.strip():
            raise TsaError("a TSA URL is required to obtain timestamps")
        self._url = url
        self._trust_anchors = list(trust_anchors)
        self._hash_algo = hash_algo
        self._timeout = timeout_seconds

    async def timestamp(self, message: bytes) -> tuple[bytes, VerifiedTimestamp]:
        request = build_timestamp_request(message, hash_algo=self._hash_algo)
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(
                    self._url,
                    content=request.der,
                    headers={
                        "Content-Type": CONTENT_TYPE_QUERY,
                        "Accept": CONTENT_TYPE_REPLY,
                    },
                )
        except httpx.HTTPError as exc:
            raise TsaError(f"the TSA at {self._url} could not be reached: {exc}") from exc

        if response.status_code != 200:
            raise TsaError(f"the TSA returned HTTP {response.status_code}")

        token = token_from_response(response.content)
        # The nonce is checked here, while we still hold it. It is not persisted with the anchor, so
        # this is the only moment replay protection can be enforced.
        verified = verify_timestamp_token(
            token,
            message=message,
            trust_anchors=self._trust_anchors,
            nonce=request.nonce,
        )
        return token, verified


def build_timestamp_authority(
    *,
    enabled: bool,
    url: str,
    trust_anchors_pem: str,
    hash_algo: str = "sha256",
    timeout_seconds: float = 10.0,
) -> TimestampAuthority | None:
    """Build the configured authority, or ``None`` when timestamping is off.

    ``None`` is the air-gapped answer, and it is a first-class return value rather than an error: a
    deployment with no reachable TSA must still be able to cut anchors, because WORM anchoring alone
    already defeats truncation. What it loses is proof of *when*, which ADR-0003 documents as the
    remaining residual.
    """
    if not enabled:
        return None
    return HttpTimestampAuthority(
        url,
        load_trust_anchors(trust_anchors_pem),
        hash_algo=hash_algo,
        timeout_seconds=timeout_seconds,
    )


__all__ = [
    "CONTENT_TYPE_QUERY",
    "CONTENT_TYPE_REPLY",
    "CONTENT_TYPE_TST_INFO",
    "EKU_TIME_STAMPING",
    "HttpTimestampAuthority",
    "TimestampAuthority",
    "TimestampRequest",
    "TsaError",
    "TsaVerificationError",
    "VerifiedTimestamp",
    "build_timestamp_authority",
    "build_timestamp_request",
    "certificates_to_pem",
    "load_trust_anchors",
    "token_from_response",
    "verify_timestamp_token",
]
