"""Domain aggregate invariants, with no database at all — ADR-0011 §1/§2, Wave 2.4.

Every test here constructs a declarative instance directly and calls a method on it. No session, no
engine, no fixtures, no Postgres — which is the property ADR-0011 is actually after: an invariant
that
can only be exercised through a service and a database is an invariant nobody tests exhaustively.

The value of these tests is in the *negative* cases. That an aggregate permits a legal transition
proves very little; that it refuses every illegal one, from every state, is what makes "illegal
states
cannot be constructed" a checked claim. So the state machines are tested exhaustively over their
cross-product rather than on a few happy paths.

A note on what is deliberately absent: there is no test here that a service calls these methods.
That
is not testable without the service, and it is covered by the module service tests — the point of
this
file is that the rules hold regardless of who calls them.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from sentinelai.modules.case_management.exceptions import InvalidCaseStatusTransitionError
from sentinelai.modules.case_management.models import (
    STATUS_ARCHIVED,
    STATUS_CLOSED,
    STATUS_OPEN,
    TRANSITIONS,
    VALID_STATUSES,
    Case,
)
from sentinelai.modules.ingestion.models import Evidence
from sentinelai.modules.investigation.exceptions import FindingAlreadyReviewedError
from sentinelai.modules.investigation.models import (
    REVIEW_DISPOSITIONS,
    STATUS_CONFIRMED,
    STATUS_PROPOSED,
    STATUS_REJECTED,
    Entity,
    Relationship,
)
from sentinelai.shared.cem import (
    PUBLIC_SOURCE_AUTHORITY,
    ArtifactType,
    ConfidenceScore,
    CustodyEventType,
    EvidenceCategory,
    IntegrityHash,
    LegalAuthorityRef,
)
from sentinelai.shared.exceptions import LegalHoldViolationError, ValidationFailedError

_NOW = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)


def assert_rejects(field: str, fragment: str) -> AbstractContextManager[None]:
    """Assert a ``ValidationFailedError`` naming ``field`` and explaining it with ``fragment``.

    ``ValidationFailedError`` carries a fixed message and puts the reason in ``.details``, so
    ``pytest.raises(match=...)`` would silently match nothing useful. Checking the details pins the
    field name as well as the reason — which is what an API client actually reads.
    """

    @contextmanager
    def _check() -> Iterator[None]:
        with pytest.raises(ValidationFailedError) as excinfo:
            yield
        details = excinfo.value.details or []
        assert any(
            d.get("field") == field and fragment in str(d.get("message", "")) for d in details
        ), f"expected a {field!r} detail containing {fragment!r}, got {details}"

    return _check()


# ---------------------------------------------------------------------------------------
# Value objects — ADR-0011 §2
# ---------------------------------------------------------------------------------------


class TestIntegrityHash:
    def test_a_valid_digest_is_accepted(self) -> None:
        digest = IntegrityHash("SHA-256", "a" * 64)
        assert digest.algorithm == "SHA-256"
        assert str(digest) == f"SHA-256:{'a' * 64}"

    def test_an_unsupported_algorithm_is_refused(self) -> None:
        """SHA-1 and MD5 are absent on purpose: a collidable digest is not an integrity hash."""
        for algorithm in ("SHA-1", "MD5", "sha256", ""):
            with pytest.raises(ValidationFailedError):
                IntegrityHash(algorithm, "a" * 64)

    def test_a_non_hex_digest_is_refused(self) -> None:
        for digest in ("z" * 64, "A" * 64, "a" * 63 + "!", "aa:bb"):
            with pytest.raises(ValidationFailedError):
                IntegrityHash("SHA-256", digest)

    def test_a_digest_whose_length_disagrees_with_its_label_is_refused(self) -> None:
        """The check a hand-rolled validator usually omits.

        A 64-character value labelled SHA-512 is not a truncated SHA-512 — it is a SHA-256 with the
        wrong label, and a verifier trusts the label, so it would recompute the wrong thing forever.
        """
        with assert_rejects("integrity_hash", "mislabelled"):
            IntegrityHash("SHA-512", "a" * 64)
        with assert_rejects("integrity_hash", "mislabelled"):
            IntegrityHash("SHA-256", "a" * 128)

    def test_sha512_accepts_its_own_length(self) -> None:
        assert IntegrityHash("SHA-512", "b" * 128).digest == "b" * 128

    def test_matching_compares_the_algorithm_too(self) -> None:
        """Equal digest strings under different algorithms are not a match.

        They are a collision claim nobody made — and both algorithms here produce 64 hex characters,
        so a comparison that only looked at the digest would call them equal.
        """
        as_sha256 = IntegrityHash("SHA-256", "c" * 64)
        as_sha3 = IntegrityHash("SHA-3-256", "c" * 64)
        assert as_sha256.matches(IntegrityHash("SHA-256", "c" * 64))
        assert not as_sha256.matches(as_sha3)

    def test_it_is_immutable(self) -> None:
        """Validated at construction is worthless if the value can be edited afterwards."""
        digest = IntegrityHash("SHA-256", "d" * 64)
        with pytest.raises(AttributeError):
            digest.digest = "e" * 64  # type: ignore[misc]


class TestConfidenceScore:
    @pytest.mark.parametrize("value", ["0", "0.0", "0.5", "1", "1.0", "0.999999"])
    def test_values_within_the_unit_interval_are_accepted(self, value: str) -> None:
        assert ConfidenceScore(Decimal(value)).value == Decimal(value)

    @pytest.mark.parametrize("value", ["-0.001", "1.001", "2", "-1", "100"])
    def test_values_outside_the_unit_interval_are_refused(self, value: str) -> None:
        with assert_rejects("confidence", "[0, 1]"):
            ConfidenceScore(Decimal(value))

    def test_both_boundaries_are_inclusive(self) -> None:
        """A confidence of exactly 0 or 1 is meaningful, so the interval is closed."""
        assert ConfidenceScore(Decimal("0")).value == Decimal("0")
        assert ConfidenceScore(Decimal("1")).value == Decimal("1")

    def test_nan_is_refused(self) -> None:
        """NaN passes every comparison silently, so a range check alone would admit it."""
        with assert_rejects("confidence", "must be a number"):
            ConfidenceScore(Decimal("NaN"))

    def test_a_float_is_refused(self) -> None:
        """`Decimal(0.7)` is 0.699999999999999955591079014993738383054733276367187500.

        Accepting a float would store and compare something the caller did not say, and make a
        boundary check unreliable at exactly the boundary.
        """
        with assert_rejects("confidence", "must be a Decimal"):
            ConfidenceScore(0.7)  # type: ignore[arg-type]

    def test_parsing_accepts_strings_and_ints(self) -> None:
        assert ConfidenceScore.parse("0.25").value == Decimal("0.25")
        assert ConfidenceScore.parse(1).value == Decimal("1")

    def test_parsing_refuses_nonsense(self) -> None:
        with assert_rejects("confidence", "not a number"):
            ConfidenceScore.parse("very confident")


class TestSlugVocabularies:
    @pytest.mark.parametrize("value", ["osint", "web_page", "disk_image", "threat_intel", "a1_b2"])
    def test_well_formed_slugs_are_accepted(self, value: str) -> None:
        assert str(EvidenceCategory(value)) == value
        assert str(ArtifactType(value)) == value

    @pytest.mark.parametrize(
        "value",
        [
            "",
            "OSINT",
            "web page",
            "web-page",
            "_leading",
            "trailing_",
            "double__underscore",
            "x" * 51,
        ],
    )
    def test_malformed_slugs_are_refused(self, value: str) -> None:
        """Shape, not membership.

        The vocabulary is open — a category is added by registering an attribute schema, so a closed
        enum here would make adding one a code change and would reject data a correctly-registered
        connector is entitled to send. What must not be possible is a category that differs from
        another only by case or whitespace.
        """
        with pytest.raises(ValidationFailedError):
            EvidenceCategory(value)
        with pytest.raises(ValidationFailedError):
            ArtifactType(value)


class TestLegalAuthorityRef:
    def test_a_real_citation_is_accepted(self) -> None:
        assert str(LegalAuthorityRef("Warrant 2026-CR-00481")) == "Warrant 2026-CR-00481"

    def test_the_public_source_sentinel_is_recognised(self) -> None:
        """CEM permits public-source material with no instrument, and that must be distinguishable.

        A bare string makes "lawfully collected from a public source" indistinguishable from someone
        typing anything into a mandatory field.
        """
        assert LegalAuthorityRef(PUBLIC_SOURCE_AUTHORITY).is_public_source
        assert not LegalAuthorityRef("Warrant 12").is_public_source

    @pytest.mark.parametrize("value", ["", "   ", "\t\n"])
    def test_empty_or_whitespace_is_refused(self, value: str) -> None:
        """`"   "` satisfies a `not None` check while asserting nothing."""
        with assert_rejects("legal_authority_ref", "must not be empty"):
            LegalAuthorityRef(value)

    def test_an_absurdly_long_citation_is_refused(self) -> None:
        with assert_rejects("legal_authority_ref", "at most"):
            LegalAuthorityRef("x" * 501)


class TestCustodyEventType:
    def test_every_cem_event_type_is_constructible(self) -> None:
        for value in (
            "collected",
            "ingested",
            "accessed",
            "exported",
            "transferred",
            "analyzed",
            "integrity_reverified",
            "linked_to_case",
            "unlinked_from_case",
            "legal_hold_applied",
            "legal_hold_released",
            "disposed",
        ):
            assert str(CustodyEventType(value)) == value

    def test_an_unknown_event_type_is_refused(self) -> None:
        """A closed vocabulary, unlike category — an unrecognised custody event is not
        extensibility, it is an event nothing knows how to reason about in a legal record."""
        for value in ("deleted", "DISPOSED", "", "shredded"):
            with assert_rejects("event_type", "unknown custody event"):
                CustodyEventType(value)

    def test_only_disposal_is_a_disposal(self) -> None:
        assert CustodyEventType("disposed").is_disposal
        assert not CustodyEventType("accessed").is_disposal

    def test_the_hold_state_is_three_valued(self) -> None:
        """`None` is not `False`, and conflating them would release holds by accident.

        Every `accessed` event would silently clear a legal hold if this returned a boolean.
        """
        assert CustodyEventType("legal_hold_applied").legal_hold_state is True
        assert CustodyEventType("legal_hold_released").legal_hold_state is False
        assert CustodyEventType("accessed").legal_hold_state is None
        assert CustodyEventType("disposed").legal_hold_state is None


# ---------------------------------------------------------------------------------------
# Case — the open/closed/archived machine (ADR-0011 §1)
# ---------------------------------------------------------------------------------------


def _case(status: str = STATUS_OPEN) -> Case:
    return Case(
        case_id=uuid4(),
        title="A case",
        status=status,
        owning_user_id=uuid4(),
        created_at=_NOW,
        closed_at=None,
    )


class TestCaseAggregate:
    @pytest.mark.parametrize("start", sorted(VALID_STATUSES))
    @pytest.mark.parametrize("target", sorted(VALID_STATUSES))
    def test_the_machine_is_exhaustively_enforced(self, start: str, target: str) -> None:
        """Every state against every state, rather than a few happy paths.

        This is what makes "illegal states cannot be constructed" a checked claim: the legal edges
        come from `TRANSITIONS`, and every pair outside it must raise — including the
        self-transitions
        that a naive implementation quietly allows.
        """
        case = _case(start)
        if target in TRANSITIONS[start]:
            assert case.transition_to(target, at=_NOW) == start
            assert case.status == target
        else:
            with pytest.raises(InvalidCaseStatusTransitionError):
                case.transition_to(target, at=_NOW)
            assert case.status == start, "a refused transition must not mutate the case"

    def test_an_unknown_status_is_a_validation_error_not_a_conflict(self) -> None:
        """422 and 409 mean different things: malformed input versus a legal-but-refused move."""
        with pytest.raises(ValidationFailedError):
            _case().transition_to("frozen", at=_NOW)

    def test_archived_is_terminal(self) -> None:
        case = _case(STATUS_ARCHIVED)
        for target in (STATUS_OPEN, STATUS_CLOSED, STATUS_ARCHIVED):
            with pytest.raises(InvalidCaseStatusTransitionError):
                case.transition_to(target, at=_NOW)

    def test_closing_stamps_closed_at_and_reopening_clears_it(self) -> None:
        """`closed_at` is part of the same invariant — set exactly while the case is closed."""
        case = _case()
        case.close(at=_NOW)
        assert case.closed_at == _NOW
        case.reopen(at=_NOW)
        assert case.closed_at is None

    def test_archiving_a_closed_case_leaves_closed_at_intact(self) -> None:
        """An archived case was still closed at a point in time; archiving does not unclose it."""
        case = _case()
        case.close(at=_NOW)
        case.archive(at=_NOW)
        assert case.status == STATUS_ARCHIVED
        assert case.closed_at == _NOW


# ---------------------------------------------------------------------------------------
# Evidence — custody, legal hold, supersession (ADR-0011 §1)
# ---------------------------------------------------------------------------------------


def _evidence(*, legal_hold: bool = False, **overrides: object) -> Evidence:
    fields: dict[str, object] = {
        "evidence_id": uuid4(),
        "schema_version": "1.0.0",
        "category": "osint",
        "artifact_type": "web_page",
        "title": "An item",
        "source": {"system": "x"},
        "collected_at": _NOW,
        "ingested_at": _NOW,
        "attributes": {},
        "confidence": Decimal("0.8"),
        "status": "validated",
        "retention_policy_ref": "default",
        "legal_hold": legal_hold,
    }
    fields.update(overrides)
    return Evidence(**fields)


class TestEvidenceAggregate:
    def test_an_ordinary_custody_event_asserts_nothing_about_holds(self) -> None:
        evidence = _evidence()
        assert evidence.apply_custody_event(CustodyEventType("accessed")) is None

    def test_applying_a_hold_returns_the_new_state(self) -> None:
        evidence = _evidence()
        assert evidence.apply_custody_event(CustodyEventType("legal_hold_applied")) is True
        assert evidence.apply_custody_event(CustodyEventType("legal_hold_released")) is False

    def test_disposal_under_legal_hold_is_refused(self) -> None:
        """security-architecture.md §39: every deletion/purge path checks the hold first."""
        with pytest.raises(LegalHoldViolationError):
            _evidence(legal_hold=True).apply_custody_event(CustodyEventType("disposed"))

    def test_disposal_without_a_hold_is_permitted(self) -> None:
        assert _evidence(legal_hold=False).apply_custody_event(CustodyEventType("disposed")) is None

    def test_a_hold_does_not_block_anything_other_than_disposal(self) -> None:
        """A legal hold preserves evidence; it does not make it unreadable.

        Blocking `accessed` or `exported` under hold would stop the very review a hold exists to
        enable, so the gate is narrow on purpose.
        """
        held = _evidence(legal_hold=True)
        for event in ("accessed", "exported", "analyzed", "integrity_reverified", "linked_to_case"):
            held.apply_custody_event(CustodyEventType(event))

    def test_the_guard_can_be_asked_before_doing_the_work(self) -> None:
        """Signing and hashing a ledger entry is expensive; a caller may check first."""
        with pytest.raises(LegalHoldViolationError):
            _evidence(legal_hold=True).assert_can_record_custody(CustodyEventType("disposed"))
        _evidence(legal_hold=False).assert_can_record_custody(CustodyEventType("disposed"))

    def test_a_second_supersession_is_refused(self) -> None:
        from sentinelai.modules.ingestion.exceptions import EvidenceAlreadySupersededError

        with pytest.raises(EvidenceAlreadySupersededError):
            _evidence().assert_supersedable(already_superseded=True)

    def test_a_first_supersession_is_permitted(self) -> None:
        _evidence().assert_supersedable(already_superseded=False)

    def test_supersedability_ignores_the_status_column(self) -> None:
        """CEM §12 makes `superseded` a *derived* state (ADR-0015): the genesis value never changes.

        Trusting the column would let a second supersession through on any row whose overlay has not
        been applied — so the decision comes from the replacement-row fact alone.
        """
        stale = _evidence(status="superseded")
        stale.assert_supersedable(already_superseded=False)

    def test_the_integrity_value_object_is_exposed_when_well_formed(self) -> None:
        evidence = _evidence(integrity_algorithm="SHA-256", integrity_hash="f" * 64)
        digest = evidence.integrity
        assert digest is not None
        assert digest.matches(IntegrityHash("SHA-256", "f" * 64))

    def test_a_payloadless_item_has_no_integrity_hash(self) -> None:
        assert _evidence().integrity is None

    def test_malformed_stored_integrity_reads_as_none_rather_than_raising(self) -> None:
        """This is a read of history, and history can contain a row written before the value object.

        Constructing an invalid `IntegrityHash` to represent bad stored data would defeat the type's
        only purpose; raising would make a display path fail on a row it merely wanted to show.
        """
        assert _evidence(integrity_algorithm="SHA-256", integrity_hash="nonsense").integrity is None
        assert _evidence(integrity_algorithm="MD5", integrity_hash="a" * 32).integrity is None
        assert _evidence(integrity_algorithm="SHA-256", integrity_hash=None).integrity is None


# ---------------------------------------------------------------------------------------
# Findings — the review machine (ADR-0011 §1, PRD FR-7.3)
# ---------------------------------------------------------------------------------------


def _entity(status: str = STATUS_PROPOSED) -> Entity:
    return Entity(
        entity_id=uuid4(),
        entity_type="person",
        canonical_name="A Person",
        status=status,
        confidence=Decimal("0.7"),
        created_by_type="system",
        created_by_ref=uuid4(),
    )


def _relationship(status: str = STATUS_PROPOSED) -> Relationship:
    return Relationship(
        relationship_id=uuid4(),
        type="associated_with",
        from_entity_id=uuid4(),
        to_entity_id=uuid4(),
        directional=True,
        status=status,
        confidence=Decimal("0.7"),
        created_by_type="system",
        created_by_ref=uuid4(),
    )


class TestFindingReview:
    @pytest.mark.parametrize("disposition", sorted(REVIEW_DISPOSITIONS))
    def test_a_proposed_finding_accepts_either_disposition(self, disposition: str) -> None:
        for finding in (_entity(), _relationship()):
            assert finding.review(disposition) == STATUS_PROPOSED
            assert finding.status == disposition

    @pytest.mark.parametrize("already", [STATUS_CONFIRMED, STATUS_REJECTED])
    @pytest.mark.parametrize("disposition", sorted(REVIEW_DISPOSITIONS))
    def test_a_reviewed_finding_cannot_be_reviewed_again(
        self, already: str, disposition: str
    ) -> None:
        """Both dispositions are terminal — FR-7.3's audit trail of who decided what is the point.

        Includes re-confirming an already-confirmed finding, which is the case an implementation
        that
        only guarded against *changing* a disposition would let through.
        """
        for finding in (_entity(already), _relationship(already)):
            with pytest.raises(FindingAlreadyReviewedError):
                finding.review(disposition)
            assert finding.status == already, "a refused review must not mutate the finding"

    @pytest.mark.parametrize("disposition", ["proposed", "maybe", "", "CONFIRMED", "deleted"])
    def test_a_disposition_outside_the_vocabulary_is_refused(self, disposition: str) -> None:
        """`proposed` is refused too: a review must reach a decision, not restate the question."""
        with pytest.raises(ValidationFailedError):
            _relationship().review(disposition)

    def test_the_vocabulary_check_precedes_the_state_check(self) -> None:
        """A malformed request should be told it is malformed, whatever the finding's state.

        Reversing the order answers "already reviewed" to a request that was invalid regardless.
        """
        with pytest.raises(ValidationFailedError):
            _relationship(STATUS_CONFIRMED).review("nonsense")


class TestSupportingEvidenceInvariant:
    def test_a_relationship_requires_at_least_one_supporting_evidence(self) -> None:
        """CEM §1.6/§13: no relationship may *exist* without a supporting evidence reference."""
        with assert_rejects("evidence_ids", "≥1 supporting evidence"):
            Relationship.assert_supporting_evidence(0)

    @pytest.mark.parametrize("count", [1, 2, 50])
    def test_any_positive_count_satisfies_it(self, count: int) -> None:
        Relationship.assert_supporting_evidence(count)

    def test_it_is_a_creation_rule_not_a_review_gate(self) -> None:
        """Deliberate placement, and the one thing an implementer is most likely to get wrong.

        CEM §13's validation table says *Reject* — the rule is about existence. Enforcing it at
        confirmation would be too late (the unsupported row already exists) and would perversely
        refuse to let an analyst *reject* an unsupported finding, which is exactly what should
        happen
        to one. So `review()` does not consult it.
        """
        unsupported = _relationship()
        assert unsupported.review(STATUS_REJECTED) == STATUS_PROPOSED
        assert unsupported.status == STATUS_REJECTED
