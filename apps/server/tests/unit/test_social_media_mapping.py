"""Unit tests for the social_media capture rules and CEM mapping — api-design.md §4.6, CEM §13.

Pure functions, so no database and no service. The end-to-end path through `ingestion` is proven
against real Postgres in `test_social_media_db.py`.

The test this file exists for is `test_legal_authority_is_never_defaulted`. Social media spans a
public post and a direct message obtained under a production order, and nothing in the content says
which — so the capture has to. A default here would stamp "no authority required" on a DM.
"""

from __future__ import annotations

import pathlib
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest

import sentinelai
from sentinelai.modules.social_media.models import CapturedContent
from sentinelai.modules.social_media.service import (
    CATEGORY_SOCIAL_MEDIA,
    CONTENT_KINDS,
    map_content_to_cem,
    validate_captured_at,
    validate_content_kind,
)
from sentinelai.shared.cem import PUBLIC_SOURCE_AUTHORITY
from sentinelai.shared.exceptions import ValidationFailedError

_NOW = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)
_ACCOUNT_ID = uuid4()


@contextmanager
def _assert_rejects(field: str, fragment: str = "") -> Any:
    """Assert a 422 naming ``field``.

    ``ValidationFailedError`` carries a fixed message and puts the reason in ``.details``, so
    ``pytest.raises(match=...)`` would match nothing useful.
    """
    with pytest.raises(ValidationFailedError) as caught:
        yield
    details = caught.value.details
    assert any(
        detail["field"] == field and fragment.lower() in str(detail["message"]).lower()
        for detail in details
    ), f"expected a rejection of {field!r} containing {fragment!r}, got {details}"


def _envelope(**overrides: Any) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "schema_version": "1.0.0",
        "title": "Post by @suspect_01",
        "attributes": {"body": "meet at the usual spot", "like_count": 4},
        "confidence": 0.9,
        "legal_authority_ref": "PRODUCTION-ORDER-2026-88",
    }
    raw.update(overrides)
    return raw


def _content(**overrides: Any) -> CapturedContent:
    fields: dict[str, Any] = {
        "content_id": uuid4(),
        "status": "captured",
        "collected_at": _NOW,
        "platform": "X",
        "account_handle": "@suspect_01",
        "content_kind": "post",
        "raw_attributes": _envelope(),
    }
    fields.update(overrides)
    return CapturedContent(**fields)


# --- the content vocabulary -------------------------------------------------
def test_the_documented_kinds_are_accepted() -> None:
    """CEM §6's `social_media_intelligence` row, verbatim."""
    assert {
        "post",
        "profile_snapshot",
        "comment",
        "direct_message",
        "network_connection_snapshot",
        "media_upload",
    } == CONTENT_KINDS
    for kind in CONTENT_KINDS:
        validate_content_kind(kind)


@pytest.mark.parametrize("kind", ["", "tweet", "story", "disk_image", "ioc"])
def test_a_kind_outside_cem_6_is_refused(kind: str) -> None:
    """§4.6's validation rule. `tweet` and `story` are the plausible wrong answers — platform
    vocabulary rather than the CEM's — and `disk_image`/`ioc` belong to other categories."""
    with _assert_rejects("content_kind", "must be a CEM"):
        validate_content_kind(kind)


def test_every_accepted_kind_has_a_registered_attribute_schema() -> None:
    """The two lists that must not drift apart.

    `ingest_evidence` refuses an unregistered `(schema_version, category, artifact_type)` triple,
    so a kind this module accepts but the registry does not know is a capture that can never be
    published — and the failure would surface at publish time, far from the edit that caused it.

    Loaded by path because a migration is a standalone module, not an importable package member —
    which is also why reading its list directly, rather than copying it here, is the only way this
    assertion can actually catch a drift.
    """
    import importlib.util

    path = (
        pathlib.Path(sentinelai.__file__).parent
        / "modules/ingestion/migrations/versions/202609290003_ingestion_seed_social.py"
    )
    spec = importlib.util.spec_from_file_location("seed_social", path)
    assert spec is not None and spec.loader is not None
    seed = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(seed)

    assert set(seed._ARTIFACT_TYPES) == CONTENT_KINDS
    assert seed._CATEGORY == CATEGORY_SOCIAL_MEDIA


# --- the capture clock ------------------------------------------------------
def test_a_past_capture_is_accepted() -> None:
    validate_captured_at(_NOW - timedelta(days=30), now=_NOW)


def test_a_capture_inside_the_skew_tolerance_is_accepted() -> None:
    """A connector's clock is not ours, and the tolerance matches `ingestion`'s own — so a capture
    accepted here cannot be refused downstream by CEM §13's `collected_at` rule for the same
    reason, which would reject it only after it had been stored."""
    validate_captured_at(_NOW + timedelta(minutes=4), now=_NOW)


def test_a_future_capture_is_refused() -> None:
    """§4.6: "`captured_at` not in the future".

    It becomes the evidence object's `collected_at` — what a timeline is built from and what a
    defence would examine — so a backdated or forward-dated capture is not a cosmetic error.
    """
    with _assert_rejects("captured_at", "future"):
        validate_captured_at(_NOW + timedelta(hours=1), now=_NOW)


# --- what the mapping produces ----------------------------------------------
def test_the_mapping_derives_category_and_provenance_server_side() -> None:
    """A connector must not be able to relabel its own category.

    §13 keys the legal-authority requirement on the category, so a caller who could set it could
    relabel their way out of needing one.
    """
    evidence = map_content_to_cem(_content(), _ACCOUNT_ID)

    assert evidence.category == CATEGORY_SOCIAL_MEDIA
    assert evidence.artifact_type == "post"
    assert evidence.source == {
        "system": "X",
        "collector_id": str(_ACCOUNT_ID),
        "collection_method": "social_media_capture",
    }
    assert evidence.collected_at == _NOW


def test_an_unmonitored_handle_falls_back_to_the_handle_as_collector() -> None:
    """A connector watching a hashtag captures content from handles nobody registered.

    CEM §13 requires *a* `collector_id`, and the handle is the only stable identifier available.
    Weaker provenance than a monitored account's id, and deliberately distinguishable from it.
    """
    evidence = map_content_to_cem(_content(), None)

    assert evidence.source["collector_id"] == "@suspect_01"


def test_the_envelope_supplies_the_fields_the_columns_do_not_model() -> None:
    content = _content(raw_attributes=_envelope(description="Captured during live monitoring"))

    evidence = map_content_to_cem(content, _ACCOUNT_ID)

    assert evidence.schema_version == "1.0.0"
    assert evidence.title == "Post by @suspect_01"
    assert evidence.description == "Captured during live monitoring"
    assert evidence.attributes == {"body": "meet at the usual spot", "like_count": 4}


def test_confidence_is_parsed_as_decimal_not_float() -> None:
    """`Decimal(str(value))`, never `Decimal(value)`: a JSON `0.9` parses to a float, and
    `Decimal(0.9)` is 0.9000000000000000222… — which fails `EvidenceCreate`'s `le=1` bound only
    sometimes and only at the boundary, the worst kind of intermittent rejection."""
    evidence = map_content_to_cem(_content(raw_attributes=_envelope(confidence=0.9)), None)

    assert evidence.confidence == Decimal("0.9")
    assert isinstance(evidence.confidence, Decimal)


@pytest.mark.parametrize(
    "value", ["high", None, 1.5, -0.1], ids=["text", "absent", "over", "under"]
)
def test_an_unusable_confidence_is_refused(value: Any) -> None:
    with pytest.raises(ValidationFailedError):
        map_content_to_cem(_content(raw_attributes=_envelope(confidence=value)), None)


# --- what the mapping refuses -----------------------------------------------
@pytest.mark.parametrize(
    "key", ["schema_version", "title", "attributes", "confidence", "legal_authority_ref"]
)
def test_a_missing_envelope_field_is_refused_by_name(key: str) -> None:
    """FR-1.3: a rejection names every failed rule per field, never a silent partial ingest."""
    raw = _envelope()
    del raw[key]

    with _assert_rejects(f"raw_attributes.{key}", "required"):
        map_content_to_cem(_content(raw_attributes=raw), _ACCOUNT_ID)


def test_legal_authority_is_never_defaulted() -> None:
    """**The test this module exists to have.**

    CEM §13 lists `social_media_intelligence` among the categories requiring a legal authority. The
    `public_source_no_authority_required` sentinel is a *permitted value* — a public post genuinely
    needs no warrant — but assuming it would stamp "no authority required" on a direct message
    obtained under a production order. The platform cannot tell the two apart from the content, so
    the capture must say, and an absent value is a refusal rather than a default.
    """
    raw = _envelope()
    del raw["legal_authority_ref"]

    with _assert_rejects("raw_attributes.legal_authority_ref"):
        map_content_to_cem(_content(raw_attributes=raw), _ACCOUNT_ID)


def test_the_public_source_sentinel_is_accepted_when_stated() -> None:
    """The other half: a connector capturing a public post says so explicitly, and that is valid.

    Asserted so a later "require a real warrant reference" tightening cannot quietly make lawful
    open-source capture impossible.
    """
    content = _content(raw_attributes=_envelope(legal_authority_ref=PUBLIC_SOURCE_AUTHORITY))

    assert map_content_to_cem(content, None).legal_authority_ref == PUBLIC_SOURCE_AUTHORITY


def test_every_missing_field_is_reported_at_once() -> None:
    """A connector fixing a rejected publish should learn all of it in one response."""
    with pytest.raises(ValidationFailedError) as caught:
        map_content_to_cem(_content(raw_attributes={}), None)

    assert {detail["field"] for detail in caught.value.details} == {
        "raw_attributes.schema_version",
        "raw_attributes.title",
        "raw_attributes.attributes",
        "raw_attributes.confidence",
        "raw_attributes.legal_authority_ref",
    }


@pytest.mark.parametrize("value", ["a string", 42, ["a", "list"]], ids=["str", "int", "list"])
def test_non_object_attributes_are_refused(value: Any) -> None:
    with _assert_rejects("raw_attributes.attributes", "object"):
        map_content_to_cem(_content(raw_attributes=_envelope(attributes=value)), None)


def test_payload_and_integrity_fields_pass_through_when_present() -> None:
    """A captured image has bytes; a post's text does not. Both are valid — CEM §13 asks for
    integrity fields on *payload-bearing* evidence, and `ingestion` recomputes them (ADR-0008)."""
    digest = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    content = _content(
        content_kind="media_upload",
        raw_attributes=_envelope(
            payload_ref="s3://sentinelai-evidence/social/img.jpg",
            integrity_algorithm="SHA-256",
            integrity_hash=digest,
        ),
    )

    evidence = map_content_to_cem(content, None)

    assert evidence.payload_ref == "s3://sentinelai-evidence/social/img.jpg"
    assert evidence.integrity_algorithm == "SHA-256"
    assert evidence.integrity_hash == digest
    assert map_content_to_cem(_content(), None).payload_ref is None
