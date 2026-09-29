"""Unit tests for evidence extraction — CEM §7, §8, §10.

Pure functions, so no database, no service, no extractor instance for most of it. The end-to-end
path through a real correlation run is proven against Postgres in `test_correlation_run_db.py`.

The tests this file exists for are the **near-miss** ones. An extractor that finds evil.example in a
chat log is easy; one that does not also propose `4.5` from a tool's version string, or file a
40-character Ethereum address as a SHA-1, is the difference between a review queue an analyst works
through and one they abandon.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest

from sentinelai.modules.investigation.extraction import (
    CO_OCCURRENCE_CONFIDENCE,
    CO_OCCURRENCE_REL_TYPE,
    ENTITY_ACCOUNT,
    ENTITY_DIGITAL_ASSET,
    ENTITY_FINANCIAL_INSTRUMENT,
    IDENTIFIER_CONFIDENCE,
    MAX_ENTITIES_PER_EVIDENCE,
    EvidenceRecord,
    ExtractedEntity,
    ExtractedRelationship,
    Extraction,
    HeuristicIdentifierExtractor,
    classify,
    tokenize,
    validate_extraction,
)

_SHA256 = "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"
_SHA1 = "aaf4c61ddcc5e8a2dabede0f3b482cd9aea9434d"
_MD5 = "5d41402abc4b2a76b9719d911017c592"
_ETH = "0x5aaeb6053f3e94c9b9a09f33669435e7ef1beaed"
_BTC = "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa"


def _record(**overrides: Any) -> EvidenceRecord:
    fields: dict[str, Any] = {
        "evidence_id": uuid4(),
        "category": "digital_forensics",
        "artifact_type": "chat_message",
        "title": "Chat export",
        "description": None,
        "attributes": {},
    }
    fields.update(overrides)
    return EvidenceRecord(**fields)


# --- classification ---------------------------------------------------------
@pytest.mark.parametrize(
    ("token", "entity_type", "canonical"),
    [
        ("EVIL-C2.Example", ENTITY_DIGITAL_ASSET, "evil-c2.example"),
        ("192.168.1.10", ENTITY_DIGITAL_ASSET, "192.168.1.10"),
        ("2001:0db8::1", ENTITY_DIGITAL_ASSET, "2001:db8::1"),
        (_SHA256.upper(), ENTITY_DIGITAL_ASSET, _SHA256),
        (_SHA1, ENTITY_DIGITAL_ASSET, _SHA1),
        (_MD5, ENTITY_DIGITAL_ASSET, _MD5),
        ("http://Evil.Example/Payload", ENTITY_DIGITAL_ASSET, "http://evil.example/Payload"),
        ("Suspect.01@Mail.Example", ENTITY_ACCOUNT, "suspect.01@mail.example"),
        (_ETH.upper().replace("0X", "0x"), ENTITY_FINANCIAL_INSTRUMENT, _ETH),
        (_BTC, ENTITY_FINANCIAL_INSTRUMENT, _BTC),
    ],
)
def test_each_identifier_lands_on_its_cem_7_type(
    token: str, entity_type: str, canonical: str
) -> None:
    """CEM §7's taxonomy, applied. The canonical name is the *normalized* form, not the raw string —
    which is what makes the same identifier arriving twice converge on one node."""
    entity = classify(token)

    assert entity is not None
    assert entity.entity_type == entity_type
    assert entity.canonical_name == canonical


def test_a_url_keeps_its_path_and_is_not_reduced_to_its_host() -> None:
    """The path is part of what was observed: `/invoice.pdf` and `/payload.exe` on one host are two
    different things to an investigator, and collapsing them would lose the distinction."""
    entity = classify("https://cdn.example/a/payload.exe")

    assert entity is not None
    assert entity.canonical_name == "https://cdn.example/a/payload.exe"


def test_a_url_path_keeps_its_case_while_the_host_folds() -> None:
    """A host is case-insensitive per DNS; a path is not, and folding it names a different
    resource."""
    entity = classify("https://CDN.Example/Case/File.PDF")

    assert entity is not None
    assert entity.canonical_name == "https://cdn.example/Case/File.PDF"


def test_an_ethereum_address_is_not_filed_as_a_sha1() -> None:
    """**The near-miss this ordering exists for.** Both are 40 hexadecimal characters; only the `0x`
    separates them, and a wallet address recorded as a file hash is a finding pointing at the wrong
    kind of thing entirely."""
    assert classify(_ETH) == ExtractedEntity(
        ENTITY_FINANCIAL_INSTRUMENT, _ETH, IDENTIFIER_CONFIDENCE
    )
    assert classify(_SHA1) == ExtractedEntity(ENTITY_DIGITAL_ASSET, _SHA1, IDENTIFIER_CONFIDENCE)


def test_a_bitcoin_address_is_matched_case_sensitively() -> None:
    """Base58 is case-sensitive. Folding it would both accept invalid addresses and store a
    canonical name that is not the address."""
    assert classify(_BTC) is not None
    assert classify(_BTC.lower()) is None


def test_an_email_is_an_account_not_its_domain() -> None:
    """The address is what an investigator cares about; the domain check runs after so the
    right-hand side cannot swallow it."""
    entity = classify("suspect@mail.example")

    assert entity is not None
    assert entity.entity_type == ENTITY_ACCOUNT
    assert entity.canonical_name == "suspect@mail.example"


@pytest.mark.parametrize(
    "token",
    ["4.5", "1.2.3", "v2.0", "report.1", "", "hello", "127.0.0.256", "a@b", "0x123"],
    ids=[
        "version",
        "triple",
        "vprefix",
        "numeric-tld",
        "empty",
        "word",
        "bad-ip",
        "bad-email",
        "short-hex",
    ],
)
def test_a_near_miss_is_not_an_entity(token: str) -> None:
    """**The test that keeps the review queue usable.**

    Without the "final label must be alphabetic" rule, `4.5` and `1.2.3` are hostnames — and a
    forensic tool's version string in `attributes` becomes an entity an analyst must reject, once
    per
    evidence item, forever.
    """
    assert classify(token) is None


def test_a_filename_with_an_alphabetic_extension_is_a_digital_asset() -> None:
    """Accepted deliberately, and it is not a false positive: CEM §7 defines `digital_asset` as "A
    file, domain, IP, URL, or indicator", so a filename is squarely in the type. It is noisier
    than a
    hash, which is what `proposed` status and analyst review are for."""
    entity = classify("payload.exe")

    assert entity is not None
    assert entity.entity_type == ENTITY_DIGITAL_ASSET


def test_confidence_is_certainty_of_presence_not_of_relevance() -> None:
    """1.000 says "this identifier is in this record and is what it was classified as" — a decided
    question. Whether it *matters* is the analyst's call, which is why the finding is still
    `proposed`. Scoring it lower would make `min_confidence` hide findings that are certainly
    there."""
    entity = classify(_SHA256)

    assert entity is not None
    assert entity.confidence == Decimal("1.000")


# --- tokenization -----------------------------------------------------------
def test_free_text_and_a_structured_value_take_the_same_path() -> None:
    """Which one a connector happened to use must not decide whether the identifier is found."""
    assert list(tokenize("contacted evil.example yesterday")) == [
        "contacted",
        "evil.example",
        "yesterday",
    ]
    assert list(tokenize("evil.example")) == ["evil.example"]


@pytest.mark.parametrize(
    "text",
    ["visit evil.example.", "(evil.example)", '"evil.example",', "evil.example;"],
    ids=["sentence", "parens", "quoted", "semicolon"],
)
def test_surrounding_punctuation_is_trimmed(text: str) -> None:
    assert "evil.example" in list(tokenize(text))


def test_machine_joined_identifiers_are_separated() -> None:
    """A single field holding `a@x.example,b@y.example` is common in exported headers."""
    tokens = list(tokenize("a@x.example,b@y.example"))

    assert tokens == ["a@x.example", "b@y.example"]


# --- the adapter ------------------------------------------------------------
async def test_the_adapter_reads_title_description_and_attributes() -> None:
    """All three, because a connector's own summary is often where the identifier is stated
    plainly."""
    extractor = HeuristicIdentifierExtractor()

    extraction = await extractor.extract(
        _record(
            title="Beacon to evil-c2.example",
            description=f"payload {_SHA256}",
            attributes={"sender": "suspect@mail.example"},
        )
    )

    assert {entity.canonical_name for entity in extraction.entities} == {
        "evil-c2.example",
        _SHA256,
        "suspect@mail.example",
    }


async def test_attribute_keys_are_never_read_as_evidence() -> None:
    """A key names a field. `{"evil.example": "1"}` is a field called `evil.example`, not an
    observation of the domain."""
    extraction = await HeuristicIdentifierExtractor().extract(
        _record(title="Export", attributes={"evil.example": 1})
    )

    assert extraction.entities == ()


async def test_nested_attributes_are_walked() -> None:
    extraction = await HeuristicIdentifierExtractor().extract(
        _record(attributes={"messages": [{"body": "send to 10.0.0.5"}]})
    )

    assert [entity.canonical_name for entity in extraction.entities] == ["10.0.0.5"]


async def test_the_same_identifier_twice_is_one_entity() -> None:
    """Deduplicated on `(entity_type, canonical_name)`, which is also the key the service resolves
    against — so a record repeating an address does not propose it twice."""
    extraction = await HeuristicIdentifierExtractor().extract(
        _record(
            title="evil.example",
            description="EVIL.example again",
            attributes={"a": "evil.example"},
        )
    )

    assert len(extraction.entities) == 1


async def test_co_occurrence_is_the_complete_pairwise_set() -> None:
    """CEM §10's "co-occurrence within the same evidence item", as §8's `associated_with`.

    Complete over the entities kept, never a subset: a truncated clique would leave an analyst
    unable to tell which associations the run considered from which it silently dropped.
    """
    extraction = await HeuristicIdentifierExtractor().extract(
        _record(attributes={"a": "one.example", "b": "two.example", "c": "three.example"})
    )

    assert len(extraction.entities) == 3
    assert len(extraction.relationships) == 3  # 3 choose 2
    assert {edge.rel_type for edge in extraction.relationships} == {CO_OCCURRENCE_REL_TYPE}
    assert {edge.confidence for edge in extraction.relationships} == {CO_OCCURRENCE_CONFIDENCE}
    assert not any(edge.directional for edge in extraction.relationships)


async def test_a_single_identifier_produces_no_edge() -> None:
    """Nothing co-occurred with it. The entity is still a finding — it is grounded in the evidence
    by a MENTIONS edge, which is CEM §11's provenance, not a relationship."""
    extraction = await HeuristicIdentifierExtractor().extract(_record(title="only evil.example"))

    assert len(extraction.entities) == 1
    assert extraction.relationships == ()


async def test_an_empty_extraction_is_an_ordinary_answer() -> None:
    extraction = await HeuristicIdentifierExtractor().extract(
        _record(title="Interview notes", attributes={"summary": "no identifiers were mentioned"})
    )

    assert extraction.is_empty


async def test_extraction_is_capped_per_evidence_item() -> None:
    """**Because co-occurrence is quadratic.** Fifty identifiers in one document would be 1,225
    machine guesses a human has to dispose of (PRD FR-7.3), from a single record."""
    many = {f"f{index}": f"host{index}.example" for index in range(60)}

    extraction = await HeuristicIdentifierExtractor().extract(_record(attributes=many))

    assert len(extraction.entities) == MAX_ENTITIES_PER_EVIDENCE
    expected_pairs = MAX_ENTITIES_PER_EVIDENCE * (MAX_ENTITIES_PER_EVIDENCE - 1) // 2
    assert len(extraction.relationships) == expected_pairs


async def test_the_adapter_is_deterministic() -> None:
    """Two runs over the same record must converge, not propose variants of one finding."""
    record = _record(
        title="Beacon to evil-c2.example", attributes={"hash": _SHA256, "wallet": _ETH}
    )
    extractor = HeuristicIdentifierExtractor()

    first = await extractor.extract(record)
    second = await extractor.extract(record)

    assert first == second


def test_the_adapter_names_itself_with_a_version() -> None:
    """It becomes §25.8's `generated_by`, which an analyst reads months later to know what proposed
    a finding — so an unversioned name would answer half the question."""
    assert HeuristicIdentifierExtractor().name == "heuristic-identifier-extraction/1"


# --- the port's guard -------------------------------------------------------
def test_a_well_formed_extraction_has_no_problems() -> None:
    extraction = Extraction(
        entities=(
            ExtractedEntity(ENTITY_DIGITAL_ASSET, "a.example", Decimal("1.0")),
            ExtractedEntity(ENTITY_DIGITAL_ASSET, "b.example", Decimal("1.0")),
        ),
        relationships=(ExtractedRelationship(CO_OCCURRENCE_REL_TYPE, 0, 1, False, Decimal("0.5")),),
    )

    assert validate_extraction(extraction) == []


@pytest.mark.parametrize(
    ("relationship", "fragment"),
    [
        (
            ExtractedRelationship(CO_OCCURRENCE_REL_TYPE, 0, 9, False, Decimal("0.5")),
            "not an entity index",
        ),
        (
            ExtractedRelationship(CO_OCCURRENCE_REL_TYPE, 0, 0, False, Decimal("0.5")),
            "relate to itself",
        ),
        (
            ExtractedRelationship(CO_OCCURRENCE_REL_TYPE, 0, 1, False, Decimal("2")),
            "between 0 and 1",
        ),
    ],
    ids=["dangling", "self-loop", "confidence"],
)
def test_a_malformed_relationship_is_named(
    relationship: ExtractedRelationship, fragment: str
) -> None:
    """A guard on the **port**, not on today's adapter — which cannot produce any of these. A
    model-backed adapter can, and a dangling endpoint index must be refused before it writes a
    finding into a legal record."""
    extraction = Extraction(
        entities=(
            ExtractedEntity(ENTITY_DIGITAL_ASSET, "a.example", Decimal("1.0")),
            ExtractedEntity(ENTITY_DIGITAL_ASSET, "b.example", Decimal("1.0")),
        ),
        relationships=(relationship,),
    )

    problems = validate_extraction(extraction)

    assert any(fragment in problem for problem in problems)


def test_a_blank_entity_name_is_refused() -> None:
    extraction = Extraction(
        entities=(ExtractedEntity(ENTITY_DIGITAL_ASSET, "   ", Decimal("1.0")),)
    )

    assert any("must not be blank" in problem for problem in validate_extraction(extraction))
