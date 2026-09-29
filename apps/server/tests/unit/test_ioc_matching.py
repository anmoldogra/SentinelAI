"""IOC validation and evidence tokenization — api-design.md §4.4, event-driven §25.4.

Pure tests: no database, no fixtures. Everything here is a property of the normalizer or the
tokenizer, and the cases that matter are the ones a substring match gets wrong — because a false
positive is not cosmetic here. It tells an analyst that a known-malicious indicator appears in a
piece of evidence, and that claim lands in a legal record.
"""

from __future__ import annotations

import pytest

from sentinelai.modules.threat_intel.matching import (
    INDICATOR_DOMAIN,
    INDICATOR_HASH_MD5,
    INDICATOR_HASH_SHA1,
    INDICATOR_HASH_SHA256,
    INDICATOR_IPV4,
    INDICATOR_IPV6,
    INDICATOR_TYPES,
    INDICATOR_URL,
    InvalidIndicator,
    candidate_tokens,
    normalize_indicator,
)

_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
_SHA1 = "da39a3ee5e6b4b0d3255bfef95601890afd80709"
_MD5 = "d41d8cd98f00b204e9800998ecf8427e"


# --- the vocabulary ---------------------------------------------------------
def test_the_indicator_vocabulary_is_the_documented_one() -> None:
    """api-design.md §4.4 fixes the set. It is closed, unlike CEM's open category vocabulary,
    because each value selects a validator and a normalizer — an unrecognised type is not
    extensibility, it is an indicator nothing knows how to compare."""
    assert {
        "ipv4",
        "ipv6",
        "domain",
        "url",
        "hash_md5",
        "hash_sha1",
        "hash_sha256",
    } == INDICATOR_TYPES


def test_an_unknown_type_is_refused() -> None:
    with pytest.raises(InvalidIndicator):
        normalize_indicator("carrier_pigeon", "anything")


@pytest.mark.parametrize("indicator_type", sorted(INDICATOR_TYPES))
def test_an_empty_value_is_refused_for_every_type(indicator_type: str) -> None:
    with pytest.raises(InvalidIndicator):
        normalize_indicator(indicator_type, "   ")


# --- hashes -----------------------------------------------------------------
@pytest.mark.parametrize(
    ("indicator_type", "value"),
    [
        (INDICATOR_HASH_MD5, _MD5),
        (INDICATOR_HASH_SHA1, _SHA1),
        (INDICATOR_HASH_SHA256, _SHA256),
    ],
)
def test_a_hash_lower_cases(indicator_type: str, value: str) -> None:
    """Hex is case-insensitive, so the same digest written two ways is one indicator. Without
    folding, an IOC registered upper-case would never match evidence recording it lower-case."""
    assert normalize_indicator(indicator_type, value.upper()) == value


@pytest.mark.parametrize(
    ("indicator_type", "wrong"),
    [
        (INDICATOR_HASH_SHA256, _MD5),
        (INDICATOR_HASH_MD5, _SHA256),
        (INDICATOR_HASH_SHA1, _SHA256),
    ],
)
def test_a_mislabelled_hash_is_refused(indicator_type: str, wrong: str) -> None:
    """A 32-character digest labelled `hash_sha256` is not a truncated SHA-256 — it is an MD5 with
    the wrong label, and the label is what decides which evidence field this is ever compared
    against. Same argument `IntegrityHash` makes in `shared/cem.py`."""
    with pytest.raises(InvalidIndicator):
        normalize_indicator(indicator_type, wrong)


def test_a_non_hex_hash_is_refused() -> None:
    with pytest.raises(InvalidIndicator):
        normalize_indicator(INDICATOR_HASH_MD5, "z" * 32)


# --- addresses --------------------------------------------------------------
def test_ipv4_normalizes() -> None:
    assert normalize_indicator(INDICATOR_IPV4, " 192.0.2.10 ") == "192.0.2.10"


@pytest.mark.parametrize("bad", ["192.0.2.256", "192.0.2", "not-an-ip", "2001:db8::1"])
def test_an_invalid_ipv4_is_refused(bad: str) -> None:
    """Including a valid IPv6 address: it is not an IPv4, and accepting it under the wrong label
    would store an indicator whose type lies about what it is."""
    with pytest.raises(InvalidIndicator):
        normalize_indicator(INDICATOR_IPV4, bad)


def test_ipv6_collapses_to_one_canonical_form() -> None:
    """The reason IPs go through `ipaddress` rather than a string compare: these three spellings are
    one host, and a textual comparison would treat them as three."""
    forms = ["2001:0db8:0000:0000:0000:0000:0000:0001", "2001:db8::1", "2001:DB8::0:1"]
    assert {normalize_indicator(INDICATOR_IPV6, form) for form in forms} == {"2001:db8::1"}


# --- domains ----------------------------------------------------------------
def test_a_domain_lower_cases_and_drops_a_trailing_dot() -> None:
    """DNS is case-insensitive and `example.com.` is the same name as `example.com` — the fully
    qualified form with an explicit root."""
    assert normalize_indicator(INDICATOR_DOMAIN, "Evil.Example.COM.") == "evil.example.com"


@pytest.mark.parametrize(
    "bad",
    [
        "localhost",  # a single label: not registerable as a threat indicator
        "-leading.example.com",
        "trailing-.example.com",
        "has space.example.com",
        "under_score.example.com",
        "",
    ],
)
def test_an_invalid_domain_is_refused(bad: str) -> None:
    with pytest.raises(InvalidIndicator):
        normalize_indicator(INDICATOR_DOMAIN, bad)


# --- URLs -------------------------------------------------------------------
def test_a_url_folds_host_but_not_path() -> None:
    """A path *is* case-sensitive. Folding it would make an IOC match a different resource on the
    same host, which is a false positive with a plausible-looking URL attached to it."""
    assert (
        normalize_indicator(INDICATOR_URL, "HTTP://Evil.Example.COM/Payload.EXE")
        == "http://evil.example.com/Payload.EXE"
    )


def test_a_url_keeps_its_query() -> None:
    assert (
        normalize_indicator(INDICATOR_URL, "https://evil.example.com/a?id=7")
        == "https://evil.example.com/a?id=7"
    )


@pytest.mark.parametrize("bad", ["evil.example.com/path", "://nohost", "http://"])
def test_a_url_without_a_scheme_and_host_is_refused(bad: str) -> None:
    with pytest.raises(InvalidIndicator):
        normalize_indicator(INDICATOR_URL, bad)


# --- tokenization: the false-positive cases ---------------------------------
def test_a_longer_domain_does_not_yield_a_shorter_one() -> None:
    """**The test this module exists for.** ``ioc.value in str(attributes)`` would match the IOC
    `evil.com` against evidence mentioning `notevil.com` and `evil.com.br`. Exact comparison on a
    token does not, and the difference is a false accusation in a case record."""
    tokens = candidate_tokens({"domain": "notevil.com", "other": "evil.com.br"})

    assert "evil.com" not in tokens
    assert "notevil.com" in tokens
    assert "evil.com.br" in tokens


def test_a_url_contributes_its_host() -> None:
    """Evidence recording `http://evil.com/x` should match a `domain` IOC for `evil.com`: the
    analyst registered the domain as malicious and the URL demonstrates it. Not decomposing would
    make the match depend on whether the connector stored a URL or a hostname."""
    tokens = candidate_tokens({"url": "http://Evil.com/payload"})

    assert "evil.com" in tokens
    assert "http://evil.com/payload" in tokens


def test_tokens_are_found_in_nested_structures() -> None:
    """Connector output is nested, and an indicator buried three levels down is still present in the
    evidence."""
    tokens = candidate_tokens(
        {
            "observations": [
                {"network": {"peer": "192.0.2.10"}},
                {"files": [{"sha256": _SHA256.upper()}]},
            ]
        }
    )

    assert "192.0.2.10" in tokens
    assert _SHA256 in tokens, "hex folds to lower case, matching how the IOC is stored"


def test_an_ipv6_in_evidence_matches_its_canonical_form() -> None:
    """The IOC is stored canonicalized, so the evidence token has to be canonicalized the same way
    or the two never meet."""
    tokens = candidate_tokens({"peer": "2001:0DB8::0:1"})
    assert normalize_indicator(INDICATOR_IPV6, "2001:db8::1") in tokens


def test_tokenization_is_depth_bounded() -> None:
    """The structure is connector-supplied and this runs on every ingest, so a pathological payload
    must cost a bounded walk. A value past the depth limit is simply not a candidate — which is a
    missed match, not a crash, and the bound is generous relative to real evidence shapes."""
    deep: dict[str, object] = {"v": "192.0.2.1"}
    for _ in range(12):
        deep = {"nest": deep}

    assert candidate_tokens(deep) == set()


def test_tokenization_is_breadth_bounded() -> None:
    """Likewise in breadth: one payload must not be able to slow every match run."""
    wide = {f"k{i}": f"host{i}.example.com" for i in range(5_000)}
    tokens = candidate_tokens(wide)
    assert 0 < len(tokens) <= 2_000 + 1


@pytest.mark.parametrize("empty", [{}, {"a": None}, {"a": ""}, [], "", None])
def test_empty_attributes_yield_no_tokens(empty: object) -> None:
    """A scan of an evidence object with nothing string-like in it should do no work, not raise —
    `attributes` is connector-supplied and its shape is not guaranteed."""
    assert candidate_tokens(empty) == set()


def test_numbers_and_booleans_are_ignored() -> None:
    """Only strings can be indicators. Stringifying a number would let the port `443` match an
    indicator that happened to be spelled the same way."""
    assert candidate_tokens({"port": 443, "flag": True, "ratio": 1.5}) == set()
