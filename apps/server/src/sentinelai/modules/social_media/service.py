"""social_media business logic (guide Part 5) — account monitoring, content capture, and
publication into the canonical evidence model.

The last of the four domain-producer modules, and the same shape as the three before it: a rich
record table holds what the connector captured, and publishing maps it onto the CEM through
`ingestion.public`. CEM §9's pipeline splits identically — Extract is the connector's, Map is this
module's, Enrich/Validate/Commit are `ingestion`'s.

**The rule worth stating up front: legal authority is required and must be stated, not assumed.**
CEM §13 lists `social_media_intelligence` among the categories requiring
`classification.legal_authority_ref`. The sentinel `public_source_no_authority_required` *is* a
permitted value — a public post genuinely needs no warrant — but it is never defaulted here, and
that is the whole point. Social media spans a public tweet and a direct message obtained under a
production order, and the platform cannot tell which from the content. So the capture must say. An
`osint`-style default would quietly stamp "no authority required" on a DM, which is the one mistake
in this module that could put a prosecution at risk rather than merely produce a bad record.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Final
from uuid import UUID

from fastapi import Depends

from sentinelai.modules.ingestion.public import (
    EvidenceCreate,
    EvidenceService,
    get_evidence_service,
)
from sentinelai.modules.social_media.events import (
    EVENT_ACCOUNT_REGISTERED,
    EVENT_CONTENT_CAPTURED,
)
from sentinelai.modules.social_media.exceptions import (
    ContentAlreadyPublishedError,
    ContentNotFoundError,
)
from sentinelai.modules.social_media.models import CapturedContent, SocialAccountObserved
from sentinelai.modules.social_media.repository import SocialMediaUnitOfWork, get_social_media_uow
from sentinelai.modules.social_media.schemas import AccountCreate, ContentCreate
from sentinelai.platform.auth.audit import record_audit_event
from sentinelai.platform.auth.dependencies import CurrentUser
from sentinelai.platform.crypto import get_kms
from sentinelai.platform.crypto.kms import KeyManagementService
from sentinelai.shared.exceptions import ValidationFailedError
from sentinelai.shared.pagination import PageParams, decode_cursor, encode_cursor

_MODULE = "social_media"

# CEM §5's category for this module's output. Fixed, not client-supplied: content that arrived
# through the social-media surface is social-media evidence, and letting a payload say otherwise
# would let a connector mislabel its own provenance — and, because §13 keys the legal-authority
# requirement on the category, relabel its way out of needing one.
CATEGORY_SOCIAL_MEDIA: Final = "social_media_intelligence"

# CEM §6's artifact types for that category, verbatim. §4.6's validation rule is that
# `content_kind` is one of these. All six are registered in `ingestion.attribute_schema_registry`
# by `202609290003_ingest_seed_social` — without that migration every publish here would be refused
# for an unregistered triple, however valid the kind.
CONTENT_KINDS: Final[frozenset[str]] = frozenset(
    {
        "post",
        "profile_snapshot",
        "comment",
        "direct_message",
        "network_connection_snapshot",
        "media_upload",
    }
)

# The content lifecycle. §3.3 requires a `status` column and does not fix its vocabulary; these
# match §25.6's event name (`content_captured`) so the row state and the announced fact cannot drift
# into describing different things — the same reasoning `osint` records for its findings.
STATUS_CAPTURED: Final = "captured"
STATUS_PUBLISHED: Final = "published"

# §4.6: "`captured_at` not in the future". A little tolerance because a connector's clock is not
# ours, and matching `ingestion`'s own skew allowance means a capture this module accepts cannot
# then be refused by CEM §13's `collected_at`/`ingested_at` rule downstream — two gates disagreeing
# about one clock would reject a valid capture only after it had been stored.
CAPTURE_CLOCK_SKEW: Final = timedelta(minutes=5)

# What `raw_attributes` must carry before a capture can be mapped onto the CEM. See
# `map_content_to_cem`.
_REQUIRED_FOR_PUBLISH: Final[tuple[str, ...]] = (
    "schema_version",
    "title",
    "attributes",
    "confidence",
    "legal_authority_ref",
)


# -- CEM mapping (§9 step 2) -------------------------------------------------------------------
#
# Module-level, like `forensics`: neither function touches service state, and the validation a
# connector hits most often should be reachable without constructing a service around an object
# storage client and a KMS it never calls.
def validate_content_kind(content_kind: str) -> None:
    """§4.6's rule: ``content_kind`` ∈ CEM §6's ``social_media_intelligence`` artifact types.

    Enforced at capture rather than only at publish, so a connector learns its vocabulary is wrong
    while it still has the content in hand — and because a kind outside the set has no registered
    attributes schema, so it could never be published anyway.
    """
    if content_kind not in CONTENT_KINDS:
        raise ValidationFailedError(
            [
                {
                    "field": "content_kind",
                    "message": (
                        "must be a CEM §6 artifact type for social_media_intelligence; "
                        f"permitted: {sorted(CONTENT_KINDS)}"
                    ),
                }
            ]
        )


def validate_captured_at(captured_at: datetime, *, now: datetime | None = None) -> None:
    """§4.6's rule: ``captured_at`` is not in the future.

    A capture timestamped ahead of the clock is either a broken connector or a backdating attempt,
    and both matter here: `captured_at` becomes the evidence object's `collected_at`, which is what
    a timeline is built from and what a defence would examine.
    """
    reference = now or datetime.now(UTC)
    if captured_at > reference + CAPTURE_CLOCK_SKEW:
        raise ValidationFailedError(
            [{"field": "captured_at", "message": "must not be in the future"}]
        )


def map_content_to_cem(content: CapturedContent, account_id: UUID | None) -> EvidenceCreate:
    """Map captured content onto a canonical evidence object.

    **`raw_attributes` is the capture's envelope**, the same fixed-envelope decision `osint` made
    for the column of that name and `forensics` had to press `device_info` into. CEM §9 step 2 asks
    for a "connector mapping profile — a versioned, declarative field-mapping definition, **not**
    per-connector business logic embedded in the ingestion path", and `database-design.md` §3.2
    records a `mapping_profile_version` while modelling no table to hold the profiles. So the
    connector declares the CEM fields §3.3's columns do not model, and anything missing is a `422`
    naming it — which keeps the mapping declarative, keeps §9's Validate step loud (FR-1.3), and
    leaves the profile store a recorded gap rather than an invented table.

    **`legal_authority_ref` is required with no default**, per the module docstring. The sentinel is
    accepted when the connector states it; it is never assumed.

    **Provenance is derived from the capture's own columns.** `source.system` is the platform, and
    `collector_id` is the monitored account's id when this handle is one the platform watches —
    which ties the evidence to the monitoring configuration that produced it and survives a handle
    being renamed. A capture for an unmonitored handle falls back to the handle itself, because CEM
    §13 requires *a* collector and the handle is the only stable identifier there is; recorded
    explicitly rather than treated as equivalent, since the two have different evidential weight.
    """
    raw = content.raw_attributes if isinstance(content.raw_attributes, dict) else {}
    missing = [key for key in _REQUIRED_FOR_PUBLISH if raw.get(key) is None]
    if missing:
        raise ValidationFailedError(
            [
                {
                    "field": f"raw_attributes.{key}",
                    "message": "required to map this capture onto the canonical evidence model",
                }
                for key in missing
            ]
        )

    attributes = raw["attributes"]
    if not isinstance(attributes, dict):
        raise ValidationFailedError(
            [{"field": "raw_attributes.attributes", "message": "must be an object"}]
        )

    return EvidenceCreate(
        schema_version=str(raw["schema_version"]),
        category=CATEGORY_SOCIAL_MEDIA,
        artifact_type=content.content_kind,
        title=str(raw["title"]),
        description=str(raw["description"]) if raw.get("description") is not None else None,
        source={
            "system": content.platform,
            "collector_id": str(account_id) if account_id is not None else content.account_handle,
            # CEM §9's OSINT/social pipeline is a connector capture, and the value names the act
            # rather than the tool — the tool is `system`.
            "collection_method": "social_media_capture",
        },
        collected_at=content.collected_at,
        attributes=attributes,
        confidence=_decimal_confidence(raw["confidence"]),
        payload_ref=str(raw["payload_ref"]) if raw.get("payload_ref") is not None else None,
        integrity_algorithm=(
            str(raw["integrity_algorithm"]) if raw.get("integrity_algorithm") else None
        ),
        integrity_hash=str(raw["integrity_hash"]) if raw.get("integrity_hash") else None,
        legal_authority_ref=str(raw["legal_authority_ref"]),
    )


def _decimal_confidence(value: Any) -> Decimal:
    """Parse a confidence as ``Decimal``, never ``float``.

    ``Decimal(str(value))`` rather than ``Decimal(value)``: a JSON body parses `0.9` into a Python
    float, and `Decimal(0.9)` is 0.9000000000000000222…, which fails `EvidenceCreate`'s `le=1` bound
    only sometimes and only at the boundary — the worst kind of intermittent rejection.
    """
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise ValidationFailedError(
            [{"field": "raw_attributes.confidence", "message": "must be a number in [0, 1]"}]
        ) from None
    if not (Decimal(0) <= parsed <= Decimal(1)):
        raise ValidationFailedError(
            [{"field": "raw_attributes.confidence", "message": "must be in [0, 1]"}]
        )
    return parsed


class SocialMediaService:
    def __init__(
        self,
        uow: SocialMediaUnitOfWork,
        *,
        evidence: EvidenceService,
        kms: KeyManagementService,
    ) -> None:
        self._uow = uow
        # `ingestion`'s service, via its public interface. Injected rather than constructed so this
        # module never learns how to build one — that is `ingestion`'s composition concern.
        self._evidence = evidence
        # Required, not optional: §4.6 makes both POSTs audited, and every audit entry is signed
        # (ADR-0003 §1), so an optional KMS would make an unsigned one reachable.
        self._kms = kms

    # -- accounts -----------------------------------------------------------
    async def list_accounts(self, actor: CurrentUser) -> Sequence[SocialAccountObserved]:
        return await self._uow.accounts.list_()

    async def register_account(
        self, data: AccountCreate, actor: CurrentUser, correlation_id: str
    ) -> SocialAccountObserved:
        """Put an account under monitoring, or refresh one already monitored.

        **Converges on ``(platform, handle)`` rather than refusing a duplicate**, because the table
        is a set of accounts *observed*: `@handle` on one platform is one account, and its
        `last_observed_at` column exists to be moved. A caller asking to monitor an account that is
        already monitored has its intent satisfied, so answering `409` would make the client treat
        a no-op as an error.

        The refresh is what makes the convergence meaningful rather than a silent nothing, and
        ``uq_social_account_platform_handle`` is what makes it safe when two registrations race:
        both can find nothing, and only one insert then succeeds.

        Publishes ``social_media.account_registered`` (§25.6) **only for a genuinely new account** —
        its trigger is "New account added for monitoring", and announcing a refresh as a new
        registration would make a consumer counting monitored accounts wrong.
        """
        now = datetime.now(UTC)
        existing = await self._uow.accounts.find_by_platform_and_handle(data.platform, data.handle)
        if existing is not None:
            existing.last_observed_at = now
            await self._audit(
                actor,
                "social_account_observed",
                existing.account_id,
                {"platform": data.platform, "handle": data.handle},
            )
            return existing

        account = SocialAccountObserved(
            platform=data.platform,
            handle=data.handle,
            first_observed_at=now,
            last_observed_at=now,
        )
        await self._uow.accounts.add(account)
        await self._uow.outbox.publish(
            event_type=EVENT_ACCOUNT_REGISTERED,
            aggregate_type="social_account",
            aggregate_id=account.account_id,
            payload={"account_id": str(account.account_id), "platform": account.platform},
            correlation_id=correlation_id,
            actor_type="user",
            actor_ref=actor.user_id,
        )
        await self._audit(
            actor,
            "social_account_registered",
            account.account_id,
            {"platform": data.platform, "handle": data.handle},
        )
        return account

    # -- content ------------------------------------------------------------
    async def list_content(
        self, actor: CurrentUser, page: PageParams
    ) -> tuple[Sequence[CapturedContent], str | None, bool]:
        """One page of captured content, with the cursor for the next (api-design.md §2.5)."""
        after: tuple[datetime, UUID] | None = None
        if page.cursor is not None:
            # The cursor carries the sort value as an ISO string and the column is `timestamptz`, so
            # it has to be parsed back before the row-value comparison can be typed at all.
            sort_value, last_id = decode_cursor(page.cursor)
            after = (datetime.fromisoformat(sort_value), last_id)
        # One extra row, so "is there a next page" is answered by the query rather than guessed.
        rows = list(await self._uow.content.list_(limit=page.limit + 1, after=after))
        has_more = len(rows) > page.limit
        items = rows[: page.limit]
        next_cursor = (
            encode_cursor(items[-1].collected_at.isoformat(), items[-1].content_id)
            if has_more and items
            else None
        )
        return items, next_cursor, has_more

    async def get_content(self, content_id: UUID, actor: CurrentUser) -> CapturedContent:
        content = await self._uow.content.get_by_id(content_id)
        if content is None:
            raise ContentNotFoundError()
        return content

    async def create_content(
        self, data: ContentCreate, actor: CurrentUser, correlation_id: str
    ) -> CapturedContent:
        """Record a capture. It is **not** evidence yet (§3.3's nullable ``evidence_id``).

        Publishes ``social_media.content_captured`` (§25.6), whose trigger is "Connector or manual
        entry captures content" — so it fires **here**, on capture, not on publication.
        api-design.md §4.6 says "Events Published: none at this step" for the same endpoint; they
        cannot both hold, and `CLAUDE.md` makes `event-driven-architecture.md` authoritative for the
        event catalog. The identical conflict was resolved the identical way for
        `osint.finding_captured`, `threat_intel.ioc_registered` and
        `forensics.artifact_registered` — so the platform has one rule about which document wins,
        not four.

        The monitored account is **not** required to exist. A connector watching a hashtag or a
        thread legitimately captures content from handles nobody registered, and refusing it would
        lose evidence to a bookkeeping gap. `map_content_to_cem` records the weaker provenance that
        results.
        """
        validate_content_kind(data.content_kind)
        validate_captured_at(data.captured_at)

        content = CapturedContent(
            status=STATUS_CAPTURED,
            collected_at=data.captured_at,
            platform=data.platform,
            account_handle=data.account_handle,
            content_kind=data.content_kind,
            raw_attributes=data.raw_attributes,
        )
        await self._uow.content.add(content)

        # An observed handle's monitoring window moves when it produces content, which is what
        # `last_observed_at` is for. Only for a handle already monitored: capturing from a hashtag
        # must not silently enrol its author into a monitoring list an analyst curates.
        account = await self._uow.accounts.find_by_platform_and_handle(
            data.platform, data.account_handle
        )
        if account is not None:
            account.last_observed_at = datetime.now(UTC)

        await self._uow.outbox.publish(
            event_type=EVENT_CONTENT_CAPTURED,
            aggregate_type="social_content",
            aggregate_id=content.content_id,
            payload={
                "content_id": str(content.content_id),
                "platform": content.platform,
                "account_handle": content.account_handle,
            },
            correlation_id=correlation_id,
            actor_type="user",
            actor_ref=actor.user_id,
        )
        await self._audit(
            actor,
            "social_content_captured",
            content.content_id,
            {"platform": data.platform, "content_kind": data.content_kind},
        )
        return content

    async def publish_content(
        self, content_id: UUID, actor: CurrentUser, correlation_id: str
    ) -> CapturedContent:
        """Normalize the capture into ``ingestion.evidence`` (CEM §9, api-design.md §4.6).

        Idempotency is **natural**, as for every domain producer's publish: the check is on the
        capture's own ``evidence_id``, so a retry returns `409` rather than creating a second
        evidence object for one post. That is why §4.6 marks this endpoint idempotent with no key.

        Ordering is the safety property. `ingestion` commits the evidence row *before* this capture
        records the ``evidence_id``, both inside the request's single transaction (ADR-0005), so the
        two cannot disagree: if the mapping fails §13's rules the error propagates, the transaction
        rolls back, and the capture stays unpublished — FR-1.3's "never a silent partial ingestion".

        No event is published here. §25.6 defines two events for this module and publication
        triggers neither; the canonical fact is `evidence.ingested`, which `ingestion` publishes on
        its own. Inventing a third would violate `CLAUDE.md` rule 1.
        """
        content = await self.get_content(content_id, actor)
        if content.evidence_id is not None:
            raise ContentAlreadyPublishedError(
                f"content {content_id} was already published as evidence {content.evidence_id}"
            )

        account = await self._uow.accounts.find_by_platform_and_handle(
            content.platform, content.account_handle
        )
        evidence = await self._evidence.ingest_evidence(
            map_content_to_cem(content, account.account_id if account else None),
            actor,
            correlation_id,
        )
        content.evidence_id = evidence.evidence_id
        content.status = STATUS_PUBLISHED

        await self._audit(
            actor,
            "evidence_published_from_social_media",
            content.content_id,
            {"evidence_id": str(evidence.evidence_id), "content_kind": content.content_kind},
        )
        return content

    # -- internals ----------------------------------------------------------
    async def _audit(
        self, actor: CurrentUser, action: str, target_id: UUID, details: dict[str, object]
    ) -> None:
        roles = actor.roles
        await record_audit_event(
            self._uow.session,
            kms=self._kms,
            actor_user_id=actor.user_id,
            actor_role=roles[0] if roles else "none",
            action=action,
            module=_MODULE,
            target_type="social_content",
            target_id=target_id,
            details=details,
        )


def get_social_media_service(
    uow: SocialMediaUnitOfWork = Depends(get_social_media_uow),
    evidence: EvidenceService = Depends(get_evidence_service),
    kms: KeyManagementService = Depends(get_kms),
) -> SocialMediaService:
    """Compose the service. `evidence` arrives through `ingestion.public` — publication calls
    `ingestion` synchronously because §4.6's `200` carries the `evidence_id`, which an outbox
    hand-off cannot produce."""
    return SocialMediaService(uow, evidence=evidence, kms=kms)
