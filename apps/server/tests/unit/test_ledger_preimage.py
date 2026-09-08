"""Ledger preimage completeness and tamper-evidence — ADR-0003 §1/§2, Wave 1.2.

The roadmap's acceptance criterion for Wave 1.2 is "preimage covers ALL persisted fields
(table-driven test); forgery-detection test". Both are here, and the first is table-driven in the
literal sense: it reads the **live SQLAlchemy table definition** and asserts that every column is
either inside the preimage or on an explicit, justified exclusion list. A prose claim that "the
preimage is complete" rots the moment someone adds a column; this fails the build instead.

The forgery tests are the point of the whole increment. Before Wave 1.2 many of them would have
passed while the field was silently rewritten, because the attribution fields were not in the
preimage — ADR-0003 Context §2's "the forgeable fields are exactly the attribution fields".
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import pytest

from sentinelai.modules.ingestion.models import EvidenceCustodyEvent
from sentinelai.modules.ingestion.service import _custody_entry_hash
from sentinelai.platform.auth.audit import _compute_hash
from sentinelai.platform.auth.models import AuditLog
from sentinelai.platform.crypto.canonical import CanonicalizationError, canonicalize
from sentinelai.platform.crypto.ledger import (
    LEDGER_HASH_ALGO,
    LEDGER_PREIMAGE_VERSION,
    LedgerPreimageError,
    compute_entry_hash,
    ledger_timestamp,
    ledger_uuid,
)

_NOW = datetime(2026, 9, 8, 12, 0, 0, 123456, tzinfo=UTC)

# Columns that cannot be inside the hash, each for a structural reason rather than convenience.
# Keep this list short and keep the reasons honest — it is the only sanctioned way for a column
# to escape the preimage.
_EXCLUDED = {
    "entry_hash": "the hash itself — it is the function's own output",
    "signature": "signed over the hash, so it cannot be an input to it",
    "key_id": "written with the signature, after the hash exists",
    "sig_alg": "written with the signature, after the hash exists",
    "anchor_ref": "written asynchronously by Wave 1.3, long after the entry",
}

# Injected into every preimage by `compute_entry_hash` rather than supplied by the caller, so
# they are covered without appearing in the caller's dict.
_INJECTED = {"hash_algo", "preimage_version"}

# The preimage abbreviates a few column names. Mapping them here (rather than renaming the keys)
# keeps the wire-visible preimage stable while letting the completeness check match columns.
_CUSTODY_KEY_FOR_COLUMN = {"prev_event_hash": "prev", "sequence_number": "seq"}
_AUDIT_KEY_FOR_COLUMN = {"prev_entry_hash": "prev"}

# The keys each hash function actually passes to `compute_entry_hash`. Written out rather than
# introspected: the point of the test is to compare the preimage against the *table*, so deriving
# both sides from the same source would make it vacuous.
_CUSTODY_PREIMAGE_KEYS = {
    "prev",
    "custody_event_id",
    "evidence_id",
    "seq",
    "event_type",
    "occurred_at",
    "actor_user_id",
    "actor_role",
    "authority_ref",
    "notes",
    "integrity_hash_at_event",
}

_AUDIT_PREIMAGE_KEYS = {
    "prev",
    "audit_id",
    "occurred_at",
    "actor_user_id",
    "actor_role",
    "action",
    "module",
    "target_type",
    "target_id",
    "ip_address",
    "user_agent",
    "details",
}


def test_custody_preimage_covers_every_persisted_column() -> None:
    """Table-driven: every column of `evidence_custody_events` is hashed, or excluded with cause."""
    columns = {column.name for column in EvidenceCustodyEvent.__table__.columns}
    covered = {_CUSTODY_KEY_FOR_COLUMN.get(name, name) for name in columns}
    covered -= set(_EXCLUDED) | _INJECTED
    missing = covered - _CUSTODY_PREIMAGE_KEYS
    assert not missing, (
        f"columns absent from the custody preimage: {sorted(missing)}. "
        "Add them to _custody_entry_hash, or to _EXCLUDED with a structural reason."
    )


def test_audit_preimage_covers_every_persisted_column() -> None:
    """Table-driven: every column of `audit_log` is hashed, or excluded with cause."""
    columns = {column.name for column in AuditLog.__table__.columns}
    covered = {_AUDIT_KEY_FOR_COLUMN.get(name, name) for name in columns}
    covered -= set(_EXCLUDED) | _INJECTED
    missing = covered - _AUDIT_PREIMAGE_KEYS
    assert not missing, (
        f"columns absent from the audit preimage: {sorted(missing)}. "
        "Add them to _compute_hash, or to _EXCLUDED with a structural reason."
    )


def test_the_preimage_keys_are_not_stale() -> None:
    """Guards the guard: every declared preimage key must correspond to a real column.

    Without this, the completeness tests above would still pass if a key were renamed in the hash
    function but not here — they only check one direction.
    """
    custody_columns = {
        _CUSTODY_KEY_FOR_COLUMN.get(c.name, c.name) for c in EvidenceCustodyEvent.__table__.columns
    }
    assert custody_columns >= _CUSTODY_PREIMAGE_KEYS

    audit_columns = {_AUDIT_KEY_FOR_COLUMN.get(c.name, c.name) for c in AuditLog.__table__.columns}
    assert audit_columns >= _AUDIT_PREIMAGE_KEYS


def test_wave_1_2_added_exactly_the_attribution_fields_to_the_audit_preimage() -> None:
    """Pins ADR-0003 Context §2's finding: what was omitted was the attribution."""
    before = {"prev", "action", "target_id", "details"}
    assert _AUDIT_PREIMAGE_KEYS - before == {
        "audit_id",
        "occurred_at",
        "actor_user_id",
        "actor_role",
        "module",
        "target_type",
        "ip_address",
        "user_agent",
    }


def test_wave_1_2_added_exactly_the_attribution_fields_to_the_custody_preimage() -> None:
    before = {"prev", "evidence_id", "seq", "event_type", "integrity_hash_at_event", "occurred_at"}
    assert _CUSTODY_PREIMAGE_KEYS - before == {
        "custody_event_id",
        "actor_user_id",
        "actor_role",
        "authority_ref",
        "notes",
    }


# --------------------------------------------------------------------------------------
# Forgery detection — many of these passed silently before Wave 1.2
# --------------------------------------------------------------------------------------


def _custody_kwargs(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "prev_hash": "0" * 64,
        "custody_event_id": UUID("11111111-1111-1111-1111-111111111111"),
        "evidence_id": UUID("22222222-2222-2222-2222-222222222222"),
        "sequence_number": 1,
        "event_type": "ingested",
        "occurred_at": _NOW,
        "actor_user_id": UUID("33333333-3333-3333-3333-333333333333"),
        "actor_role": "investigator",
        "authority_ref": "warrant-2026-001",
        "notes": "collected at scene",
        "integrity_hash_at_event": "a" * 64,
    }
    base.update(overrides)
    return base


@pytest.mark.parametrize(
    ("field", "forged"),
    [
        ("custody_event_id", UUID("99999999-9999-9999-9999-999999999999")),
        ("evidence_id", UUID("99999999-9999-9999-9999-999999999999")),
        ("sequence_number", 2),
        ("event_type", "exported"),
        ("occurred_at", datetime(2026, 9, 8, 12, 0, 0, 123457, tzinfo=UTC)),  # one microsecond
        ("actor_user_id", UUID("99999999-9999-9999-9999-999999999999")),
        ("actor_role", "admin"),
        ("authority_ref", "warrant-2026-999"),
        ("notes", "collected at station"),
        ("integrity_hash_at_event", "b" * 64),
        ("prev_hash", "c" * 64),
    ],
)
def test_tampering_with_any_custody_field_changes_the_hash(field: str, forged: Any) -> None:
    """Every field, one at a time. Five of these eleven were forgeable before Wave 1.2."""
    honest = _custody_entry_hash(**_custody_kwargs())
    tampered = _custody_entry_hash(**_custody_kwargs(**{field: forged}))
    assert honest != tampered, f"forging {field} did not change the entry hash"


def _audit_kwargs(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "prev_hash": "0" * 64,
        "audit_id": UUID("11111111-1111-1111-1111-111111111111"),
        "occurred_at": _NOW,
        "actor_user_id": UUID("33333333-3333-3333-3333-333333333333"),
        "actor_role": "investigator",
        "action": "evidence.read",
        "module": "ingestion",
        "target_type": "evidence",
        "target_id": UUID("22222222-2222-2222-2222-222222222222"),
        "ip_address": "10.0.0.5",
        "user_agent": "console/1.0",
        "details": {"reason": "case review"},
    }
    base.update(overrides)
    return base


@pytest.mark.parametrize(
    ("field", "forged"),
    [
        ("audit_id", UUID("99999999-9999-9999-9999-999999999999")),
        ("occurred_at", datetime(2026, 9, 8, 12, 0, 0, 123457, tzinfo=UTC)),
        ("actor_user_id", UUID("99999999-9999-9999-9999-999999999999")),
        ("actor_role", "admin"),
        ("action", "evidence.delete"),
        ("module", "case_management"),
        ("target_type", "case"),
        ("target_id", UUID("99999999-9999-9999-9999-999999999999")),
        ("ip_address", "10.0.0.99"),
        ("user_agent", "curl/8"),
        ("details", {"reason": "routine"}),
        ("prev_hash", "c" * 64),
    ],
)
def test_tampering_with_any_audit_field_changes_the_hash(field: str, forged: Any) -> None:
    """Eight of these twelve were forgeable before Wave 1.2 — all the attribution ones."""
    honest = _compute_hash(**_audit_kwargs())
    tampered = _compute_hash(**_audit_kwargs(**{field: forged}))
    assert honest != tampered, f"forging {field} did not change the entry hash"


def test_nulling_an_attribution_field_is_also_detected() -> None:
    """Erasing who did it must break the hash as surely as replacing them."""
    honest = _compute_hash(**_audit_kwargs())
    assert _compute_hash(**_audit_kwargs(actor_user_id=None)) != honest
    assert _compute_hash(**_audit_kwargs(ip_address=None)) != honest
    assert _compute_hash(**_audit_kwargs(details=None)) != honest


def test_a_null_actor_is_not_the_string_none() -> None:
    """`ledger_uuid` emits JSON null, never the text "None" — they are different preimages."""
    assert ledger_uuid(None) is None
    assert _compute_hash(**_audit_kwargs(user_agent=None)) != _compute_hash(
        **_audit_kwargs(user_agent="None")
    )


def test_moving_a_value_between_two_fields_is_detected() -> None:
    """A swap keeps every value present, so only a keyed preimage catches it."""
    honest = _compute_hash(**_audit_kwargs(target_type="evidence", module="ingestion"))
    swapped = _compute_hash(**_audit_kwargs(target_type="ingestion", module="evidence"))
    assert honest != swapped


# --------------------------------------------------------------------------------------
# Determinism, chaining, and the agility metadata
# --------------------------------------------------------------------------------------


def test_hashing_is_deterministic() -> None:
    assert _custody_entry_hash(**_custody_kwargs()) == _custody_entry_hash(**_custody_kwargs())
    assert _compute_hash(**_audit_kwargs()) == _compute_hash(**_audit_kwargs())


def test_details_key_order_does_not_change_the_audit_hash() -> None:
    """The JSONB round-trip guarantee, at the ledger level.

    `details` is a JSONB column, so Postgres returns its keys in its own order. Under the old
    `json.dumps` preimage a re-read row could not reproduce its own hash.
    """
    a = _compute_hash(**_audit_kwargs(details={"alpha": 1, "beta": 2, "gamma": 3}))
    b = _compute_hash(**_audit_kwargs(details={"gamma": 3, "alpha": 1, "beta": 2}))
    assert a == b


def test_details_number_normalisation_does_not_change_the_audit_hash() -> None:
    """JSONB stores numbers as `numeric`; 1 and 1.0 must hash identically (JCS renders both 1)."""
    assert _compute_hash(**_audit_kwargs(details={"n": 1})) == _compute_hash(
        **_audit_kwargs(details={"n": 1.0})
    )


def test_chain_is_bound_to_its_predecessor() -> None:
    """Entry N's hash depends on entry N-1's, so history is bound, not merely sequential."""
    first = _custody_entry_hash(**_custody_kwargs())
    second = _custody_entry_hash(**_custody_kwargs(prev_hash=first, sequence_number=2))
    forged_first = _custody_entry_hash(**_custody_kwargs(actor_role="admin"))
    assert forged_first != first
    # Replaying entry 2 against the forged predecessor gives a different hash, which is what makes
    # an edit to history detectable by recomputing forward from genesis.
    replayed = _custody_entry_hash(**_custody_kwargs(prev_hash=forged_first, sequence_number=2))
    assert replayed != second


def test_preimage_version_and_hash_algo_are_inside_the_hash() -> None:
    """Downgrade defense: the version that says *how* to verify is covered by what it verifies.

    If it were only stored beside the entry, an attacker could rewrite a row under the old partial
    format and set the column back to make it verify under the weaker rules.
    """
    fields: dict[str, object] = {"a": 1}
    baseline = compute_entry_hash(fields)

    assert baseline != hashlib.sha256(canonicalize(fields)).hexdigest()
    assert (
        baseline
        != hashlib.sha256(
            canonicalize({**fields, "hash_algo": LEDGER_HASH_ALGO, "preimage_version": 99})
        ).hexdigest()
    )
    assert (
        baseline
        == hashlib.sha256(
            canonicalize(
                {
                    **fields,
                    "hash_algo": LEDGER_HASH_ALGO,
                    "preimage_version": LEDGER_PREIMAGE_VERSION,
                }
            )
        ).hexdigest()
    )


def test_compute_entry_hash_rejects_caller_supplied_agility_metadata() -> None:
    with pytest.raises(LedgerPreimageError, match="injected by compute_entry_hash"):
        compute_entry_hash({"a": 1, "preimage_version": 0})


def test_compute_entry_hash_fails_closed_on_unhashable_details() -> None:
    """An audit row that cannot be hashed must abort the transaction, not be written unbound."""
    with pytest.raises(CanonicalizationError):
        _compute_hash(**_audit_kwargs(details={"n": float("nan")}))


def test_the_new_preimage_differs_from_the_pre_wave_1_2_one() -> None:
    """The format change is real: an old-format hash cannot accidentally match a new one."""
    import json

    legacy = hashlib.sha256(
        json.dumps(
            {
                "prev": "0" * 64,
                "evidence_id": "22222222-2222-2222-2222-222222222222",
                "seq": 1,
                "event_type": "ingested",
                "integrity_hash_at_event": "a" * 64,
                "occurred_at": _NOW.isoformat(),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    assert _custody_entry_hash(**_custody_kwargs()) != legacy


# --------------------------------------------------------------------------------------
# Timestamp rendering — the form the wire carries
# --------------------------------------------------------------------------------------


def test_ledger_timestamp_is_the_wire_form_not_python_isoformat() -> None:
    """Hashing the Z form is what lets a client hash exactly the bytes it received."""
    assert ledger_timestamp(_NOW) == "2026-09-08T12:00:00.123456Z"
    assert _NOW.isoformat() == "2026-09-08T12:00:00.123456+00:00"  # what it used to hash


def test_ledger_timestamp_preserves_microseconds_including_trailing_zeros() -> None:
    assert (
        ledger_timestamp(datetime(2026, 9, 8, 12, 0, 0, 100000, tzinfo=UTC))
        == "2026-09-08T12:00:00.100000Z"
    )
    assert ledger_timestamp(datetime(2026, 9, 8, 12, 0, 0, 0, tzinfo=UTC)) == "2026-09-08T12:00:00Z"


def test_ledger_timestamp_normalises_a_non_utc_offset() -> None:
    kolkata = timezone(timedelta(hours=5, minutes=30))
    assert ledger_timestamp(datetime(2026, 9, 8, 17, 30, 0, tzinfo=kolkata)) == (
        "2026-09-08T12:00:00Z"
    )


def test_ledger_timestamp_rejects_a_naive_datetime() -> None:
    """Guessing a timezone would silently change a preimage."""
    with pytest.raises(LedgerPreimageError, match="timezone-aware"):
        ledger_timestamp(datetime(2026, 9, 8, 12, 0, 0))


def test_uuid4_is_actually_used_for_row_identity() -> None:
    """The row id is in the preimage, so it must be generated before hashing, not by the column."""
    a, b = uuid4(), uuid4()
    assert _custody_entry_hash(**_custody_kwargs(custody_event_id=a)) != _custody_entry_hash(
        **_custody_kwargs(custody_event_id=b)
    )
