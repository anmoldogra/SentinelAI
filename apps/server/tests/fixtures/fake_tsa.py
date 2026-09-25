"""A real RFC 3161 Timestamp Authority, in-process — ADR-0003 §3, Wave 1.3c.

Not a stub. This mints genuine CMS ``SignedData`` timestamp tokens with a real certificate chain and
real RSA/ECDSA signatures, because the claim under test is that
:func:`~sentinelai.platform.crypto.tsa.verify_timestamp_token` rejects forged tokens — and a stub
that
returned canned bytes would make every one of those assertions vacuous.

It is also the only way to test this at all without reaching a public TSA over the network, which no
test suite should do: it would be slow, flaky, and a silent egress path in an air-gapped build.

The knobs exist so the tests can produce tokens that are wrong in **one specific way** each: a wrong
digest, a replayed nonce, a missing EKU, a non-critical EKU, an expired certificate, an untrusted
chain, a tampered signature. Verifying a good token proves little on its own; rejecting each of
those
is what proves the checks are real.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from asn1crypto import cms as asn1_cms
from asn1crypto import core as asn1_core
from asn1crypto import tsp as asn1_tsp
from asn1crypto import x509 as asn1_x509
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

_HASHES = {"sha256": hashes.SHA256, "sha384": hashes.SHA384, "sha512": hashes.SHA512}


class _RejectionResponse(asn1_core.Sequence):
    """A ``TimeStampResp`` carrying only status, as a rejecting TSA returns."""

    _fields = [("status", asn1_tsp.PKIStatusInfo)]  # noqa: RUF012 - asn1crypto schema DSL


def _name(common_name: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])


@dataclass
class FakeTsa:
    """An in-process TSA with its own CA.

    ``digest_algo`` is the algorithm used for the SignerInfo digest; the message-imprint algorithm
    comes from whatever the request asked for, as a real TSA does.
    """

    name: str = "SentinelAI Test TSA"
    ca_name: str = "SentinelAI Test TSA Root"
    digest_algo: str = "sha256"
    # Fault injection — each one produces a token that is wrong in exactly one way.
    include_eku: bool = True
    eku_critical: bool = True
    extra_eku: bool = False
    certificate_expired: bool = False
    omit_certificates: bool = False
    corrupt_signature: bool = False
    override_digest: bytes | None = None
    override_nonce: int | None = None
    gen_time: datetime | None = None
    status: str = "granted"
    _serial: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        self._ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self._signer_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

        now = datetime.now(UTC)
        self._ca_cert = (
            x509.CertificateBuilder()
            .subject_name(_name(self.ca_name))
            .issuer_name(_name(self.ca_name))
            .public_key(self._ca_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(days=1))
            .not_valid_after(now + timedelta(days=3650))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .sign(self._ca_key, hashes.SHA256())
        )

        if self.certificate_expired:
            valid_from, valid_to = now - timedelta(days=800), now - timedelta(days=400)
        else:
            valid_from, valid_to = now - timedelta(days=1), now + timedelta(days=365)

        builder = (
            x509.CertificateBuilder()
            .subject_name(_name(self.name))
            .issuer_name(_name(self.ca_name))
            .public_key(self._signer_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(valid_from)
            .not_valid_after(valid_to)
            .add_extension(
                x509.SubjectKeyIdentifier.from_public_key(self._signer_key.public_key()),
                critical=False,
            )
        )
        if self.include_eku:
            usages = [ExtendedKeyUsageOID.TIME_STAMPING]
            if self.extra_eku:
                usages.append(ExtendedKeyUsageOID.SERVER_AUTH)
            builder = builder.add_extension(
                x509.ExtendedKeyUsage(usages), critical=self.eku_critical
            )
        self._signer_cert = builder.sign(self._ca_key, hashes.SHA256())

    # -- what a verifier needs ---------------------------------------------------------------

    @property
    def trust_anchors(self) -> list[x509.Certificate]:
        return [self._ca_cert]

    @property
    def trust_anchor_pem(self) -> str:
        return self._ca_cert.public_bytes(serialization.Encoding.PEM).decode("ascii")

    # -- issuing ----------------------------------------------------------------------------

    def timestamp(self, request_der: bytes) -> bytes:
        """Handle a ``TimeStampReq`` and return a DER ``TimeStampResp``."""
        request = asn1_tsp.TimeStampReq.load(request_der)
        imprint = request["message_imprint"]
        hash_algo = imprint["hash_algorithm"]["algorithm"].native
        digest = self.override_digest or bytes(imprint["hashed_message"].native)
        nonce = self.override_nonce if self.override_nonce is not None else request["nonce"].native

        if self.status != "granted":
            # A real rejection omits timeStampToken entirely (RFC 3161 §2.4.2 marks it OPTIONAL).
            # asn1crypto's TimeStampResp declares it required, so dumping one through that class
            # raises — hence this minimal sequence, which is the shape a real TSA actually returns
            # and which `token_from_response` has to cope with.
            return bytes(
                _RejectionResponse(
                    {"status": {"status": self.status, "fail_info": {"bad_request"}}}
                ).dump()
            )

        self._serial += 1
        tst_info = asn1_tsp.TSTInfo(
            {
                "version": "v1",
                "policy": "1.2.3.4.5",
                "message_imprint": {
                    "hash_algorithm": {"algorithm": hash_algo},
                    "hashed_message": digest,
                },
                "serial_number": self._serial,
                "gen_time": self.gen_time or datetime.now(UTC),
                "nonce": nonce,
            }
        )
        token = self._sign_tst_info(tst_info.dump())
        response = asn1_tsp.TimeStampResp(
            {"status": {"status": "granted"}, "time_stamp_token": asn1_cms.ContentInfo.load(token)}
        )
        return bytes(response.dump())

    def token_for(self, message: bytes, *, hash_algo: str = "sha256", nonce: int = 0) -> bytes:
        """Mint a token directly over ``message`` — the archived-token path, no request/response."""
        digester = hashes.Hash(_HASHES[hash_algo]())
        digester.update(message)
        tst_info = asn1_tsp.TSTInfo(
            {
                "version": "v1",
                "policy": "1.2.3.4.5",
                "message_imprint": {
                    "hash_algorithm": {"algorithm": hash_algo},
                    "hashed_message": self.override_digest or digester.finalize(),
                },
                "serial_number": secrets.randbits(32),
                "gen_time": self.gen_time or datetime.now(UTC),
                "nonce": nonce,
            }
        )
        return self._sign_tst_info(tst_info.dump())

    def _sign_tst_info(self, tst_bytes: bytes) -> bytes:
        """Wrap DER ``TSTInfo`` in a signed CMS ``ContentInfo``."""
        digester = hashes.Hash(_HASHES[self.digest_algo]())
        digester.update(tst_bytes)
        content_digest = digester.finalize()

        signed_attrs = asn1_cms.CMSAttributes(
            [
                asn1_cms.CMSAttribute(
                    {"type": "content_type", "values": ["tst_info"]},
                ),
                asn1_cms.CMSAttribute(
                    {"type": "message_digest", "values": [content_digest]},
                ),
            ]
        )
        # RFC 5652 §5.4: the signature covers the DER SET OF, not the implicit [0] tagging used
        # inside SignerInfo. Getting this wrong is the classic CMS implementation bug.
        signature = self._signer_key.sign(
            signed_attrs.dump(), padding.PKCS1v15(), _HASHES[self.digest_algo]()
        )
        if self.corrupt_signature:
            signature = bytes([signature[0] ^ 0xFF]) + signature[1:]

        signer_info = asn1_cms.SignerInfo(
            {
                "version": "v1",
                "sid": asn1_cms.SignerIdentifier(
                    name="issuer_and_serial_number",
                    value={
                        "issuer": asn1_x509.Name.load(self._signer_cert.issuer.public_bytes()),
                        "serial_number": self._signer_cert.serial_number,
                    },
                ),
                "digest_algorithm": {"algorithm": self.digest_algo},
                "signed_attrs": signed_attrs,
                "signature_algorithm": {"algorithm": "rsassa_pkcs1v15"},
                "signature": signature,
            }
        )
        certificates = (
            []
            if self.omit_certificates
            else [
                asn1_x509.Certificate.load(
                    self._signer_cert.public_bytes(serialization.Encoding.DER)
                )
            ]
        )
        signed_data = asn1_cms.SignedData(
            {
                "version": "v3",
                "digest_algorithms": [{"algorithm": self.digest_algo}],
                "encap_content_info": {
                    "content_type": "tst_info",
                    "content": asn1_core.ParsableOctetString(tst_bytes),
                },
                "certificates": certificates,
                "signer_infos": [signer_info],
            }
        )
        return bytes(
            asn1_cms.ContentInfo({"content_type": "signed_data", "content": signed_data}).dump()
        )


__all__ = ["FakeTsa"]
