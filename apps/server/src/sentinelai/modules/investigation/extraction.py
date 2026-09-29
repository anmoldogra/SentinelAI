"""Evidence to entity/relationship extraction — canonical-evidence-model.md §10.

**A port and one adapter.** CEM §10 names eight extraction targets; this module builds the seam they
arrive through and one deterministic adapter that covers the second of them — "Identifiers (emails,
phone numbers, wallet addresses, device identifiers, IPs, domains) via pattern/NER extraction from
any payload type" — plus the co-occurrence half of "Relationship inference".

**Pure by design: no session, no clock, no IO**, the same discipline `threat_intel.matching` keeps
for the same reason. Every function takes what it needs and returns a value, so the classification
rules are testable against the cases that actually bite — a version number that looks like a
hostname, an Ethereum address that looks like a SHA-1, a domain at the end of a sentence — with no
database and no fixtures. Persistence and event publication live in `correlation.py`.

**What this adapter does not do, and does not pretend to do.** Named-entity recognition (people,
organizations, locations), temporal normalization, geolocation, sentiment, cross-source correlation
and entity resolution are the other six of §10's targets. They need a model; there is no inference
client in this codebase. A regex cannot find a person's name, so this adapter does not claim to —
the ``EvidenceExtractor`` protocol exists precisely so a model-backed adapter can be added beside
this one without the correlation run changing, and the gap is recorded rather than faked.

**Why it does not reuse `threat_intel.matching.candidate_tokens`.** Two reasons, one structural and
one semantic. Structural: that is `threat_intel`'s internal module, not part of its `public.py`, and
cross-module code goes through the public interface only. Semantic: `candidate_tokens` normalizes a
string into *every* form it might equal so an indexed equality lookup can decide — it deliberately
does not classify. Extraction needs the opposite: one committed answer per token about which CEM §7
type it is, because that answer becomes an entity's ``entity_type`` and a documented filter on
``GET /cases/{case_id}/graph``.
"""

from __future__ import annotations

import contextlib
import ipaddress
import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Final, Protocol
from urllib.parse import urlsplit
from uuid import UUID

# CEM §7's entity types, verbatim, for the kinds this adapter can commit to. The taxonomy is closed
# (§7 is a table, not an example), and `entity_types` is a documented filter on the graph endpoint —
# so a value outside it would be unfilterable by any client written against the contract.
ENTITY_ACCOUNT: Final = "account"
ENTITY_DIGITAL_ASSET: Final = "digital_asset"
ENTITY_FINANCIAL_INSTRUMENT: Final = "financial_instrument"

# CEM §8's type for "these two things turned up together": `associated_with`, "Any to Any",
# "Generic, weighted association where a more specific type doesn't apply". §8's list is closed and
# holds no co-occurrence-specific type, and CEM §10 names "co-occurrence within the same evidence
# item" as an extraction target — so this is the documented edge a co-occurrence can ground, and the
# only one.
CO_OCCURRENCE_REL_TYPE: Final = "associated_with"

# **An assumption, flagged as one: no document fixes this number.** The co-occurrence itself is
# certain — both identifiers are present in the same evidence item — but what it implies about the
# two being *related* is not, which is precisely why §8 calls `associated_with` "weighted" and why
# the finding is written `proposed` for an analyst to dispose of (PRD FR-7.3). 0.500 says "grounded,
# unweighted": high enough to be returned by default, low enough that api-design.md §6's
# `min_confidence` filter excludes it at any threshold above a half. A real weight belongs to a
# model, which is where the number should come from once one exists.
CO_OCCURRENCE_CONFIDENCE: Final = Decimal("0.500")

# **1.000 is not overconfidence, and the distinction matters.** This is confidence that the
# identifier *is what it was classified as and is present in this evidence item* — a decided
# question, settled by an anchored pattern on a string that is actually in the record. It is not a
# claim that the identifier is relevant to the investigation; that is exactly what `status:
# proposed` and an analyst's review decide (PRD FR-7.3). Scoring it lower would make
# `min_confidence` hide findings that are certainly present, which is the opposite of what an
# analyst raising that threshold is asking for.
IDENTIFIER_CONFIDENCE: Final = Decimal("1.000")

# How many identifiers one evidence item may contribute. Low **because co-occurrence is quadratic**:
# n identifiers in one item produce n(n-1)/2 `associated_with` edges, and every one of them is a
# machine guess a human must dispose of (PRD FR-7.3). Twelve yields at most 66 edges from one
# document, which is already a substantial review queue; fifty would yield 1,225 from a single
# record, and no analyst works through that. Bounded and logged by the caller rather than silent, so
# the cap is visible when it bites.
MAX_ENTITIES_PER_EVIDENCE: Final = 12

# How deep to walk a nested `attributes` object, and how many strings to take from it. Bounded
# because the structure is connector-supplied: a pathological payload must cost a bounded walk, not
# an unbounded one, on a path that runs for every evidence item in a case.
_MAX_DEPTH: Final = 6
_MAX_STRINGS: Final = 2_000
# Free text is tokenized, so one string can contribute many candidates — a forensic chat export is
# one `attributes` value and tens of thousands of words.
_MAX_TOKENS: Final = 5_000

# A hostname per RFC 1123, with the final label required to be **alphabetic and at least two
# characters**. That last requirement is the whole difference between a useful extractor and a noisy
# one: without it `4.5` and `1.2.3` are "domains", and a version string in a forensic tool's
# metadata becomes an entity an analyst has to reject.
_DOMAIN: Final = re.compile(
    r"\A(?=.{1,253}\Z)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*"
    r"\.[a-z]{2,}\Z"
)
# A local part with no spaces or `@`, then a hostname. Deliberately permissive on the local part
# (RFC 5321 allows almost anything quoted) and strict on the domain, which is the half that decides
# whether this is an address at all.
_EMAIL: Final = re.compile(r"\A[^@\s]{1,64}@(?P<domain>[a-z0-9][a-z0-9.-]{0,252})\Z")
_HEX: Final = re.compile(r"\A[0-9a-f]+\Z")
# Hex digest lengths, as `threat_intel.matching` reads them: the length *is* the identification.
_HASH_LENGTHS: Final[frozenset[int]] = frozenset({32, 40, 64})
# EIP-55 addresses are 20 bytes hex-encoded behind an `0x`. The prefix is what keeps a 40-character
# Ethereum address from being classified as the SHA-1 it is otherwise indistinguishable from.
_ETH_ADDRESS: Final = re.compile(r"\A0x[0-9a-f]{40}\Z")
# Bitcoin: base58check (P2PKH/P2SH) or bech32 (P2WPKH/P2TR). **Matched against the raw token, never
# the lower-cased one** — base58 is case-sensitive, and folding it would both accept invalid
# addresses and change the canonical name stored for a valid one.
_BTC_BASE58: Final = re.compile(r"\A[13][1-9A-HJ-NP-Za-km-z]{25,39}\Z")
_BTC_BECH32: Final = re.compile(r"\Abc1[02-9ac-hj-np-z]{11,71}\Z")

# Splitting free text into candidates: whitespace plus the two separators that routinely join
# identifiers in a machine-written field (`a@b.example,c@d.example`).
_SPLIT: Final = re.compile(r"[\s,;]+")
# Punctuation trimmed from a token's ends: a domain at the end of a sentence, a URL in parentheses,
# a quoted handle. Both ends only — never the middle, which would corrupt the identifier itself.
_TRIM: Final = "\"'`.,;:!?()[]{}<>"


@dataclass(frozen=True, slots=True)
class EvidenceRecord:
    """One evidence item's text-bearing fields, as an extractor receives them.

    Deliberately not `ingestion`'s own read shape: an extractor is given the *content* to read and
    nothing else — no status, no custody, no legal-authority field. Correlation eligibility is
    decided before this point (`correlation.py`), so an adapter cannot accidentally make that
    decision, and a model-backed adapter receives no field it has no business sending to an
    inference endpoint.
    """

    evidence_id: UUID
    category: str
    artifact_type: str
    title: str
    description: str | None
    attributes: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class ExtractedEntity:
    """A candidate CEM §7 entity. ``canonical_name`` is the comparison form, not the raw string."""

    entity_type: str
    canonical_name: str
    confidence: Decimal


@dataclass(frozen=True, slots=True)
class ExtractedRelationship:
    """A candidate CEM §8 edge between two entities of the same extraction.

    **Endpoints are indices into ``Extraction.entities``, not names.** An extractor that found the
    same canonical name twice — or two entities whose names differ only in a form the persistence
    layer folds — would be ambiguous by name, and an index cannot be. It also lets the caller
    validate the reference mechanically before it writes anything.
    """

    rel_type: str
    from_index: int
    to_index: int
    directional: bool
    confidence: Decimal


@dataclass(frozen=True, slots=True)
class Extraction:
    """What one evidence item yielded. Empty is an ordinary answer, not a failure."""

    entities: tuple[ExtractedEntity, ...] = ()
    relationships: tuple[ExtractedRelationship, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not self.entities and not self.relationships


class EvidenceExtractor(Protocol):
    """The seam a correlation run reads evidence through.

    ``async`` although the only adapter today is synchronous and pure: the adapter this protocol
    exists for is an inference client, and a synchronous port would have forced every caller and
    every test to change on the day one lands.

    ``name`` becomes ``investigation.correlation_generated``'s ``generated_by`` — §25.8 specifies it
    as a "model/run reference, per CEM §10's `created_by`" — so it carries a version, because a
    finding an analyst reads months later has to say which extractor proposed it.
    """

    @property
    def name(self) -> str: ...

    async def extract(self, record: EvidenceRecord) -> Extraction: ...


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


def candidate_strings(record: EvidenceRecord) -> Iterator[str]:
    """Every string worth classifying, from the three places evidence carries text.

    ``title`` and ``description`` are included because a connector's own summary is often where an
    identifier is stated plainly ("C2 beacon to evil-c2.example"), and ``attributes`` because that
    is where the structured record lives. Only values, never keys: a key names a field, and a field
    name is not evidence of anything.
    """
    yielded = 0
    sources: tuple[Any, ...] = (record.title, record.description, record.attributes)
    for source in sources:
        for text in _strings(source):
            if yielded >= _MAX_STRINGS:
                return
            yielded += 1
            yield text


def tokenize(text: str) -> Iterator[str]:
    """Split one string into candidate identifiers, punctuation trimmed.

    Free text and a structured field value go through the same path on purpose. A connector may
    store ``"sender": "a@b.example"`` or bury the same address in a chat body, and which one it
    chose must not decide whether the identifier is found.
    """
    for raw in _SPLIT.split(text):
        token = raw.strip(_TRIM)
        if token:
            yield token


def classify(token: str) -> ExtractedEntity | None:
    """The one committed answer about a token's CEM §7 type, or ``None`` for "not an identifier".

    Order is load-bearing where two patterns overlap:

    * an ``0x``-prefixed 40-hex string is an Ethereum address and a bare one is a SHA-1; the prefix
      is the only thing separating them, so it is checked first;
    * a URL is tested before a domain, so ``http://evil.example/p`` stays one `digital_asset` rather
      than being reduced to its host — the path is part of what was observed;
    * an email is tested before a domain, because its right-hand side would otherwise match and the
      address — the thing an investigator cares about — would be lost.

    Base58 Bitcoin addresses are matched on the **raw** token. Everything else folds case first:
    hex, DNS and URL schemes/hosts are all case-insensitive, and folding gives one canonical name so
    the same identifier arriving twice converges on one node instead of two.
    """
    if _BTC_BASE58.match(token) or _BTC_BECH32.match(token):
        return ExtractedEntity(ENTITY_FINANCIAL_INSTRUMENT, token, IDENTIFIER_CONFIDENCE)

    lowered = token.lower()

    if _ETH_ADDRESS.match(lowered):
        return ExtractedEntity(ENTITY_FINANCIAL_INSTRUMENT, lowered, IDENTIFIER_CONFIDENCE)

    if len(lowered) in _HASH_LENGTHS and _HEX.match(lowered):
        return ExtractedEntity(ENTITY_DIGITAL_ASSET, lowered, IDENTIFIER_CONFIDENCE)

    if "://" in token:
        split = urlsplit(token)
        if split.scheme and split.netloc:
            # Scheme and host fold; path and query do not — a path *is* case-sensitive, and folding
            # it would name a different resource.
            rebuilt = f"{split.scheme.lower()}://{split.netloc.lower()}{split.path}"
            if split.query:
                rebuilt = f"{rebuilt}?{split.query}"
            return ExtractedEntity(ENTITY_DIGITAL_ASSET, rebuilt, IDENTIFIER_CONFIDENCE)

    email = _EMAIL.match(lowered)
    if email and _DOMAIN.match(email.group("domain")):
        # CEM §7: `account` is "A social media, email, or cloud account". An address identifies an
        # account, not a file — so it is not a `digital_asset`, and it is not a `person`, because an
        # address is not a person and asserting one from the other is an inference an analyst makes.
        return ExtractedEntity(ENTITY_ACCOUNT, lowered, IDENTIFIER_CONFIDENCE)

    with contextlib.suppress(ValueError):
        # `ipaddress` collapses `2001:db8::0:1` and `2001:0db8::1` to one form; a textual compare
        # would treat those as two hosts.
        return ExtractedEntity(
            ENTITY_DIGITAL_ASSET, str(ipaddress.ip_address(token)), IDENTIFIER_CONFIDENCE
        )

    if _DOMAIN.match(lowered):
        return ExtractedEntity(ENTITY_DIGITAL_ASSET, lowered, IDENTIFIER_CONFIDENCE)

    return None


class HeuristicIdentifierExtractor:
    """Deterministic identifier extraction and co-occurrence inference — CEM §10, no model.

    **The scaffolding adapter this MVP asks for, honest about which target it covers.** §10's
    "Identifiers ... via pattern/NER extraction" is genuinely a pattern problem for the
    machine-readable half of it, and pattern extraction over a forensic record is a real
    investigative technique rather than a placeholder — an examiner reading a chat export for wallet
    addresses is doing this by hand. What it cannot do is the other six targets, which need a model.

    Deterministic, so a second run over the same case converges instead of proposing variants of the
    same finding: the canonical name is a normalized form, and `correlation.py` resolves an existing
    entity with the same ``(entity_type, canonical_name)`` rather than inserting a duplicate.
    """

    version: Final = "1"

    @property
    def name(self) -> str:
        return f"heuristic-identifier-extraction/{self.version}"

    async def extract(self, record: EvidenceRecord) -> Extraction:
        """Classify every identifier in the record, then relate the ones that co-occur.

        The complete pairwise set over the entities kept, never a subset of it: truncating *pairs*
        would mean an arbitrary half of a clique, and an analyst could not tell which associations
        the run considered from which it silently dropped. The bound is applied to entities instead
        (``MAX_ENTITIES_PER_EVIDENCE``), where it has a meaning — "the first twelve identifiers this
        record states" — and where the caller can see and log it.
        """
        entities = self._entities(record)
        relationships = tuple(
            ExtractedRelationship(
                rel_type=CO_OCCURRENCE_REL_TYPE,
                from_index=first,
                to_index=second,
                # `associated_with` is symmetric (§8's "Any to Any").
                directional=False,
                confidence=CO_OCCURRENCE_CONFIDENCE,
            )
            for first in range(len(entities))
            for second in range(first + 1, len(entities))
        )
        return Extraction(entities=entities, relationships=relationships)

    def _entities(self, record: EvidenceRecord) -> tuple[ExtractedEntity, ...]:
        """Distinct identifiers in discovery order, capped.

        Discovery order rather than sorted, so the cap keeps "what this record leads with" instead
        of "what sorts first" — and it is stable for a stored row, because `jsonb` has its own
        canonical key order that does not change between reads.
        """
        seen: set[tuple[str, str]] = set()
        found: list[ExtractedEntity] = []
        tokens = 0
        for text in candidate_strings(record):
            for token in tokenize(text):
                tokens += 1
                if tokens > _MAX_TOKENS:
                    return tuple(found)
                entity = classify(token)
                if entity is None:
                    continue
                key = (entity.entity_type, entity.canonical_name)
                if key in seen:
                    continue
                seen.add(key)
                found.append(entity)
                if len(found) >= MAX_ENTITIES_PER_EVIDENCE:
                    return tuple(found)
        return tuple(found)


def validate_extraction(extraction: Extraction) -> Sequence[str]:
    """Reasons this extraction cannot be persisted; empty when it can.

    A guard on the *port*, not on the one adapter behind it. `HeuristicIdentifierExtractor` cannot
    produce any of these, and that is the point: a model-backed adapter returning an out-of-range
    endpoint index, a self-loop, or a blank name must be refused before it writes a finding into a
    legal record, and refused with a reason naming what was wrong.
    """
    problems: list[str] = []
    for index, entity in enumerate(extraction.entities):
        if not entity.canonical_name.strip():
            problems.append(f"entities[{index}]: canonical_name must not be blank")
        if not Decimal(0) <= entity.confidence <= Decimal(1):
            problems.append(f"entities[{index}]: confidence must be between 0 and 1")
    count = len(extraction.entities)
    for index, relationship in enumerate(extraction.relationships):
        for field, value in (
            ("from_index", relationship.from_index),
            ("to_index", relationship.to_index),
        ):
            if not 0 <= value < count:
                problems.append(f"relationships[{index}]: {field} {value} is not an entity index")
        if relationship.from_index == relationship.to_index:
            problems.append(f"relationships[{index}]: an entity cannot relate to itself")
        if not Decimal(0) <= relationship.confidence <= Decimal(1):
            problems.append(f"relationships[{index}]: confidence must be between 0 and 1")
    return problems


__all__ = [
    "CO_OCCURRENCE_CONFIDENCE",
    "CO_OCCURRENCE_REL_TYPE",
    "ENTITY_ACCOUNT",
    "ENTITY_DIGITAL_ASSET",
    "ENTITY_FINANCIAL_INSTRUMENT",
    "IDENTIFIER_CONFIDENCE",
    "MAX_ENTITIES_PER_EVIDENCE",
    "EvidenceExtractor",
    "EvidenceRecord",
    "ExtractedEntity",
    "ExtractedRelationship",
    "Extraction",
    "HeuristicIdentifierExtractor",
    "candidate_strings",
    "classify",
    "tokenize",
    "validate_extraction",
]
