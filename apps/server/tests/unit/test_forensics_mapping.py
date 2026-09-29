"""Unit tests for the forensics CEM mapping — api-design.md §4.5, CEM §6/§13/§14.

Pure functions, so no database and no service: `map_artifact_to_cem` needs an artifact and the
examiner, `category_for_kind` needs a string. The end-to-end path through `ingestion` is proven
against real Postgres in `test_forensics_db.py`.

Every test here is about a rule that has a legal consequence if it is wrong: which category an
acquisition is filed under (it decides the attributes schema *and* whether a warrant reference is
demanded), whether a warrant reference is present at all, and whether the integrity hash an examiner
typed means what its label says.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest

from sentinelai.modules.forensics.models import Artifact
from sentinelai.modules.forensics.service import (
    ACQUISITION_CONFIDENCE,
    CATEGORY_BY_KIND,
    COLLECTION_METHOD,
    category_for_kind,
    map_artifact_to_cem,
)
from sentinelai.platform.auth.dependencies import CurrentUser
from sentinelai.shared.exceptions import ValidationFailedError

_NOW = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
_EXAMINER = CurrentUser(user_id=uuid4(), roles=("investigator",))
_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


@contextmanager
def _assert_rejects(field: str, fragment: str = "") -> Any:
    """Assert a 422 naming ``field``.

    ``ValidationFailedError`` carries a fixed message and puts the reason in ``.details``, so
    ``pytest.raises(match=...)`` would match nothing useful. Checking the details pins the field
    name, which is what an examiner's client actually reads.
    """
    with pytest.raises(ValidationFailedError) as caught:
        yield
    details = caught.value.details
    assert any(
        detail["field"] == field and fragment.lower() in str(detail["message"]).lower()
        for detail in details
    ), f"expected a rejection of {field!r} containing {fragment!r}, got {details}"


def _envelope(**overrides: Any) -> dict[str, Any]:
    info: dict[str, Any] = {
        "schema_version": "1.0.0",
        "title": "Disk image of workstation WS-4471",
        "attributes": {"image_format": "E01", "sector_count": 1024},
        "legal_authority_ref": "WARRANT-2026-0417",
    }
    info.update(overrides)
    return info


def _artifact(*, kind: str = "forensic_image", **overrides: Any) -> Artifact:
    fields: dict[str, Any] = {
        "artifact_id": uuid4(),
        "status": "registered",
        "collected_at": _NOW,
        "artifact_kind": kind,
        "device_info": _envelope(),
        "acquisition_tool": "EnCase 8",
        "acquisition_hash": f"SHA-256:{_SHA256}",
    }
    fields.update(overrides)
    return Artifact(**fields)


# --- the category is derived, never declared --------------------------------
def test_a_disk_artifact_is_digital_forensics() -> None:
    assert category_for_kind("forensic_image") == "digital_forensics"


def test_a_device_extraction_is_mobile_forensics() -> None:
    """CEM §5 keeps `mobile_forensics` separate on purpose — different tools, different shapes."""
    assert category_for_kind("oxygen_extraction") == "mobile_forensics"


def test_the_two_forensic_vocabularies_do_not_overlap() -> None:
    """If a kind appeared in both lists the map would silently pick one, and the acquisition would
    be filed under a category whose attributes schema it does not satisfy."""
    assert len(CATEGORY_BY_KIND) == 16
    assert set(CATEGORY_BY_KIND.values()) == {"digital_forensics", "mobile_forensics"}


@pytest.mark.parametrize("kind", ["", "disk-image", "post", "wallet_address", "sms"])
def test_a_kind_outside_cem_6_is_refused(kind: str) -> None:
    """§4.5's validation rule. An unknown kind has no category, so it could never be published —
    saying so at registration names the problem while the examiner is still at the acquisition."""
    with _assert_rejects("artifact_kind", "must be a CEM"):
        category_for_kind(kind)


def test_a_kind_from_another_category_is_refused() -> None:
    """`post` is a real CEM §6 artifact type — for `social_media_intelligence`. This module may only
    produce the two forensic categories, so borrowing another module's vocabulary is still a 422."""
    with _assert_rejects("artifact_kind"):
        category_for_kind("post")


# --- what the mapping produces ----------------------------------------------
def test_the_mapping_derives_provenance_server_side() -> None:
    """A caller must not be able to attribute an extraction to another tool or examiner."""
    artifact = _artifact()

    evidence = map_artifact_to_cem(artifact, _EXAMINER)

    assert evidence.category == "digital_forensics"
    assert evidence.artifact_type == "forensic_image"
    assert evidence.source == {
        "system": "EnCase 8",
        "collector_id": f"examiner:{_EXAMINER.user_id}",
        "collection_method": COLLECTION_METHOD,
    }
    assert evidence.collected_at == _NOW


def test_an_acquisition_is_recorded_at_full_confidence() -> None:
    """A tool either recovered the artifact or it did not; CEM §14's example says 1.0.

    Analytical doubt belongs on the entities an analyst derives from the artifact (CEM §10), not on
    whether the bytes were read.
    """
    assert map_artifact_to_cem(_artifact(), _EXAMINER).confidence == ACQUISITION_CONFIDENCE
    assert Decimal("1.000") == ACQUISITION_CONFIDENCE


def test_the_acquisition_hash_is_split_into_the_cem_integrity_pair() -> None:
    """The hash the tool reported becomes the evidence object's integrity claim.

    `ingestion` then recomputes it from the stored bytes and rejects a mismatch (ADR-0008 §3) — so
    splitting it correctly is what connects the examiner's manifest to the server's own digest.
    """
    evidence = map_artifact_to_cem(_artifact(), _EXAMINER)

    assert evidence.integrity_algorithm == "SHA-256"
    assert evidence.integrity_hash == _SHA256


def test_a_payload_reference_is_carried_through_when_the_envelope_names_one() -> None:
    """With a `payload_ref` there are bytes for ADR-0008 to recompute; without one there are not.
    CEM §13 asks for integrity fields on *payload-bearing* evidence, so both shapes are valid."""
    with_payload = _artifact(
        device_info=_envelope(payload_ref="s3://sentinelai-evidence/images/ws-4471.e01")
    )

    assert map_artifact_to_cem(with_payload, _EXAMINER).payload_ref == (
        "s3://sentinelai-evidence/images/ws-4471.e01"
    )
    assert map_artifact_to_cem(_artifact(), _EXAMINER).payload_ref is None


def test_the_envelope_supplies_the_fields_the_columns_do_not_model() -> None:
    artifact = _artifact(device_info=_envelope(description="Acquired on site"))

    evidence = map_artifact_to_cem(artifact, _EXAMINER)

    assert evidence.schema_version == "1.0.0"
    assert evidence.title == "Disk image of workstation WS-4471"
    assert evidence.description == "Acquired on site"
    assert evidence.attributes == {"image_format": "E01", "sector_count": 1024}
    assert evidence.legal_authority_ref == "WARRANT-2026-0417"


# --- what the mapping refuses -----------------------------------------------
@pytest.mark.parametrize("key", ["schema_version", "title", "attributes", "legal_authority_ref"])
def test_a_missing_envelope_field_is_refused_by_name(key: str) -> None:
    """FR-1.3: a rejection names every failed rule per field, never a silent partial ingest."""
    info = _envelope()
    del info[key]

    with _assert_rejects(f"device_info.{key}", "required"):
        map_artifact_to_cem(_artifact(device_info=info), _EXAMINER)


def test_every_missing_field_is_reported_at_once() -> None:
    """An examiner fixing a rejected publish should learn all of it in one response."""
    with pytest.raises(ValidationFailedError) as caught:
        map_artifact_to_cem(_artifact(device_info={}), _EXAMINER)

    assert {detail["field"] for detail in caught.value.details} == {
        "device_info.schema_version",
        "device_info.title",
        "device_info.attributes",
        "device_info.legal_authority_ref",
    }


def test_a_forensic_acquisition_has_no_lawful_default_authority() -> None:
    """CEM §13 requires a legal authority for both forensic categories, and `osint`'s
    `public_source_no_authority_required` sentinel cannot apply: a device extraction is not a public
    source. So there is nothing to default to, and the mapping refuses instead of inventing one."""
    info = _envelope()
    del info["legal_authority_ref"]

    with _assert_rejects("device_info.legal_authority_ref"):
        map_artifact_to_cem(_artifact(device_info=info), _EXAMINER)


def test_an_absent_envelope_is_refused_rather_than_treated_as_empty() -> None:
    """`device_info` is nullable in §3.3, so a registered artifact legitimately has none — it just
    cannot be published yet."""
    with _assert_rejects("device_info.schema_version"):
        map_artifact_to_cem(_artifact(device_info=None), _EXAMINER)


@pytest.mark.parametrize("value", ["a string", 42, ["a", "list"]], ids=["str", "int", "list"])
def test_non_object_attributes_are_refused(value: Any) -> None:
    """`attributes` is validated against the registered schema for the triple, which presumes an
    object. A scalar would reach `ingestion` and fail there, naming ingestion's field instead."""
    with _assert_rejects("device_info.attributes", "object"):
        map_artifact_to_cem(_artifact(device_info=_envelope(attributes=value)), _EXAMINER)


@pytest.mark.parametrize(
    ("stored", "fragment"),
    [
        (f"{_SHA256}", "ALGORITHM"),
        (f"MD5:{'a' * 32}", "unsupported algorithm"),
        (f"SHA-512:{_SHA256}", "mislabelled"),
        (f"SHA-256:{_SHA256.upper()}", "lowercase"),
        ("", "ALGORITHM"),
    ],
    ids=["no-algorithm", "forbidden-algorithm", "mislabelled-length", "upper-case", "empty"],
)
def test_an_unusable_acquisition_hash_is_refused_at_mapping_time_too(
    stored: str, fragment: str
) -> None:
    """Registration validates the hash, and so does the mapping — deliberately twice.

    The column is plain text and `database-design.md` §3.3 puts no constraint on it, so a row could
    reach publication with a value this platform would no longer accept (a migration, a manual fix,
    an older client). Re-parsing means such a row fails loudly rather than handing `ingestion` an
    algorithm/digest pair assembled from something invalid.
    """
    with _assert_rejects("acquisition_hash", fragment):
        map_artifact_to_cem(_artifact(acquisition_hash=stored), _EXAMINER)
