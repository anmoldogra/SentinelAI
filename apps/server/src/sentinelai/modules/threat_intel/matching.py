"""IOC indicator validation and evidence matching — api-design.md §4.4, event-driven §25.4.

**Pure by design: no session, no clock, no IO.** Every function takes what it needs and returns a
value, so the whole module is testable against the tricky cases — a domain inside a longer domain, a
hash in the wrong case, an IP embedded in a URL — with no database and no fixtures. The persistence
and the event publishing live in `service.py`.

**Matching is exact on a normalized token, never substring.** The temptation is
``ioc.value in str(attributes)``, and it is wrong in the direction that matters: the IOC
``evil.com`` would "match" evidence mentioning ``notevil.com`` or ``evil.com.br``, and an analyst
would be told a known-malicious domain appears in a case when it does not. A false positive here is
not a cosmetic bug — it is an accusation in a legal record. So the evidence is decomposed into
candidate tokens and each is compared for equality.
"""

from __future__ import annotations

import contextlib
import ipaddress
import re
from collections.abc import Iterator
from typing import Any, Final
from urllib.parse import urlsplit

# api-design.md §4.4's closed vocabulary. A closed set, unlike CEM's open category vocabulary,
# because each value selects a *validator and a normalizer* below — an unrecognised type is not
# extensibility, it is an indicator nothing knows how to compare.
INDICATOR_IPV4: Final = "ipv4"
INDICATOR_IPV6: Final = "ipv6"
INDICATOR_DOMAIN: Final = "domain"
INDICATOR_URL: Final = "url"
INDICATOR_HASH_MD5: Final = "hash_md5"
INDICATOR_HASH_SHA1: Final = "hash_sha1"
INDICATOR_HASH_SHA256: Final = "hash_sha256"

INDICATOR_TYPES: Final[frozenset[str]] = frozenset(
    {
        INDICATOR_IPV4,
        INDICATOR_IPV6,
        INDICATOR_DOMAIN,
        INDICATOR_URL,
        INDICATOR_HASH_MD5,
        INDICATOR_HASH_SHA1,
        INDICATOR_HASH_SHA256,
    }
)

# Hex length per hash type. The length is the whole validation: a 32-character hex string labelled
# `hash_sha256` is not a truncated SHA-256, it is an MD5 with the wrong label — the same argument
# `IntegrityHash` makes in `shared/cem.py`, and it matters more here because the label decides which
# evidence field a matcher would ever compare it against.
_HASH_LENGTHS: Final[dict[str, int]] = {
    INDICATOR_HASH_MD5: 32,
    INDICATOR_HASH_SHA1: 40,
    INDICATOR_HASH_SHA256: 64,
}
_HEX = re.compile(r"\A[0-9a-f]+\Z")

# A hostname label per RFC 1123: alphanumeric with internal hyphens, 1 to 63 characters. At
# least two labels, so a bare `localhost` is not registerable as a threat indicator.
_DOMAIN = re.compile(
    r"\A(?=.{1,253}\Z)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+\Z"
)

# How deep to walk a nested `attributes` object when collecting candidates. Bounded because the
# structure is connector-supplied: a pathological or cyclic-looking payload must cost a bounded
# walk,
# not an unbounded one, on a path that runs for every ingested evidence item.
_MAX_DEPTH: Final = 6
# Likewise bounded in breadth. An evidence object with more distinct strings than this is not a
# normal record, and scanning all of them would let one payload slow every match run.
_MAX_TOKENS: Final = 2_000


class InvalidIndicator(ValueError):
    """The indicator value does not have the shape its ``indicator_type`` claims."""


def normalize_indicator(indicator_type: str, value: str) -> str:
    """Return the canonical comparison form of an indicator, or raise :class:`InvalidIndicator`.

    Normalizing at *registration* rather than at match time is what makes matching a set lookup: the
    stored value and the tokens extracted from evidence are put in the same form once, so the
    comparison is equality on an indexed column instead of a per-IOC transformation over every
    candidate.

    * hashes and domains lower-case (hex and DNS are both case-insensitive);
    * IP addresses go through :mod:`ipaddress`, which collapses ``2001:db8::0:1`` and
      ``2001:0db8::1`` to one form — a textual compare would treat those as different hosts;
    * URLs lower-case scheme and host but keep the path verbatim, because a path *is*
      case-sensitive and folding it would match a different resource.
    """
    raw = value.strip()
    if not raw:
        raise InvalidIndicator("indicator value must not be empty")

    if indicator_type in _HASH_LENGTHS:
        lowered = raw.lower()
        expected = _HASH_LENGTHS[indicator_type]
        if len(lowered) != expected or not _HEX.match(lowered):
            raise InvalidIndicator(f"{indicator_type} must be {expected} hexadecimal characters")
        return lowered

    if indicator_type == INDICATOR_IPV4:
        try:
            parsed4 = ipaddress.IPv4Address(raw)
        except ValueError as exc:
            raise InvalidIndicator(f"not a valid IPv4 address: {raw!r}") from exc
        return str(parsed4)

    if indicator_type == INDICATOR_IPV6:
        try:
            parsed6 = ipaddress.IPv6Address(raw)
        except ValueError as exc:
            raise InvalidIndicator(f"not a valid IPv6 address: {raw!r}") from exc
        return str(parsed6)

    if indicator_type == INDICATOR_DOMAIN:
        lowered = raw.lower().rstrip(".")
        if not _DOMAIN.match(lowered):
            raise InvalidIndicator(f"not a valid domain name: {raw!r}")
        return lowered

    if indicator_type == INDICATOR_URL:
        split = urlsplit(raw)
        if not split.scheme or not split.netloc:
            raise InvalidIndicator(f"URL must have a scheme and a host: {raw!r}")
        # Scheme and host fold; path, query and fragment do not.
        rebuilt = f"{split.scheme.lower()}://{split.netloc.lower()}{split.path}"
        if split.query:
            rebuilt = f"{rebuilt}?{split.query}"
        return rebuilt

    raise InvalidIndicator(f"unknown indicator type {indicator_type!r}")


def _strings(value: Any, depth: int = 0) -> Iterator[str]:
    """Yield every string in a nested structure, depth-bounded."""
    if depth > _MAX_DEPTH:
        return
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item, depth + 1)
    elif isinstance(value, list | tuple):
        for item in value:
            yield from _strings(item, depth + 1)


def candidate_tokens(attributes: Any) -> set[str]:
    """Every value in ``attributes`` that could be an indicator, in normalized comparison form.

    One evidence object yields one token set, which the caller then matches against the IOC table in
    a single indexed query. That direction matters: the alternative — loop the IOCs and test each
    against the evidence — is O(active IOCs) per ingest and gets slower as the threat library grows,
    which is exactly backwards for the thing that runs on every ingest.

    **A URL contributes its host as well as itself.** Evidence recording
    ``http://evil.com/payload`` should match a `domain` IOC for ``evil.com``: the analyst registered
    the domain as malicious, and the URL demonstrates it. Not decomposing would make the match
    depend on whether the connector happened to store a URL or a hostname.

    Tokens are added in every plausible normalized form rather than guessed at — a bare string could
    be a domain, a hash or an IP, and this function does not know which, so it normalizes what
    parses
    and lets the equality comparison decide.
    """
    tokens: set[str] = set()
    for raw in _strings(attributes):
        if len(tokens) >= _MAX_TOKENS:
            break
        candidate = raw.strip()
        if not candidate:
            continue

        # A hash or a domain both normalize by lower-casing; store that form and let the IOC
        # table's own values decide what it matches.
        tokens.add(candidate.lower())

        # An IP normalizes to its canonical text, which differs from the raw string for IPv6.
        # Most tokens are not addresses, so failing to parse one is the common case, not an error.
        with contextlib.suppress(ValueError):
            tokens.add(str(ipaddress.ip_address(candidate)))

        # A URL contributes itself *and* its host.
        if "://" in candidate:
            split = urlsplit(candidate)
            if split.scheme and split.netloc:
                host = split.hostname
                rebuilt = f"{split.scheme.lower()}://{split.netloc.lower()}{split.path}"
                if split.query:
                    rebuilt = f"{rebuilt}?{split.query}"
                tokens.add(rebuilt)
                if host:
                    tokens.add(host.lower())
    return tokens


__all__ = [
    "INDICATOR_DOMAIN",
    "INDICATOR_HASH_MD5",
    "INDICATOR_HASH_SHA1",
    "INDICATOR_HASH_SHA256",
    "INDICATOR_IPV4",
    "INDICATOR_IPV6",
    "INDICATOR_TYPES",
    "INDICATOR_URL",
    "InvalidIndicator",
    "candidate_tokens",
    "normalize_indicator",
]
