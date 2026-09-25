"""The anchor batch cutter against a live ledger — ADR-0003 §3.

This job is what arms external anchoring. IC-029 built the library, IC-030 built the verifier that
checks anchors, and neither produced one — so the single most important assertion in this file is
the
end-to-end one: after a cut, the Verification Engine reads the anchor back and reports the ledger
*verified*, and after a truncation it reports it *failed*. Anything less proves only that rows were
written.

The empty-range behaviour gets as much attention as the happy path, because it is what makes the job
safe to schedule: `AnchorBatch` refuses an empty batch outright (anchoring nothing would publish a
commitment to the empty tree and claim it covered a range), so a cutter that did not skip would
crash
on every run against a quiet ledger.

Skips cleanly when no Postgres is reachable. Never fakes a pass.
"""

from __future__ import annotations

import base64
import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from sentinelai.modules.ingestion.anchor_jobs import (
    MAX_ENTRIES_PER_ANCHOR,
    _unanchored_tail,
    cut_anchor_batches,
)
from sentinelai.modules.ingestion.models import Evidence, EvidenceCustodyEvent
from sentinelai.platform.auth.audit import record_audit_event
from sentinelai.platform.auth.ledger_verification import (
    AuditLedgerVerificationService,
    read_anchor_views,
    read_audit_chain_hashes,
)
from sentinelai.platform.auth.models import AuditLog, LedgerAnchor
from sentinelai.platform.config import settings
from sentinelai.platform.crypto.anchoring import AnchorBatch, LedgerAnchorService
from sentinelai.platform.crypto.ledger import LEDGER_AUDIT, LEDGER_CUSTODY, LedgerSigner
from sentinelai.platform.crypto.tsa import VerifiedTimestamp, verify_timestamp_token
from sentinelai.platform.crypto.verification import (
    AnchorView,
    Finding,
    VerificationState,
)
from sentinelai.platform.db.base import Base
from tests.fixtures.fake_object_storage import FakeObjectStorage
from tests.fixtures.fake_tsa import FakeTsa
from tests.fixtures.kms import kms_for_tests

_URL = os.getenv("TEST_DATABASE_URL", settings.database_url)


async def _reachable(url: str) -> bool:
    try:
        engine = create_async_engine(url, connect_args={"timeout": 3})
        try:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
        finally:
            await engine.dispose()
        return True
    except Exception:
        return False


@pytest.fixture
async def sessions() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    if not await _reachable(_URL):
        pytest.skip(f"no Postgres reachable at {_URL.split('@')[-1]} — set TEST_DATABASE_URL")

    name = f"sentinelai_cuttertest_{uuid.uuid4().hex[:8]}"
    admin = create_async_engine(_URL, isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as conn:
            await conn.execute(text(f'CREATE DATABASE "{name}"'))
    finally:
        await admin.dispose()

    engine: AsyncEngine = create_async_engine(_URL.rsplit("/", 1)[0] + f"/{name}")
    try:
        async with engine.begin() as conn:
            await conn.execute(text("CREATE SCHEMA IF NOT EXISTS platform"))
            # The custody ledger too: the cutter sweeps BOTH evidentiary ledgers in one run, so a
            # fixture with only `platform` would make every test fail on a missing relation rather
            # than on anything it was written to check.
            await conn.execute(text("CREATE SCHEMA IF NOT EXISTS ingestion"))
            await conn.run_sync(
                Base.metadata.create_all,
                tables=[
                    AuditLog.__table__,
                    LedgerAnchor.__table__,
                    Evidence.__table__,
                    EvidenceCustodyEvent.__table__,
                ],
            )
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()
        admin = create_async_engine(_URL, isolation_level="AUTOCOMMIT")
        try:
            async with admin.connect() as conn:
                await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        finally:
            await admin.dispose()


@pytest.fixture
def storage() -> FakeObjectStorage:
    return FakeObjectStorage()


def _ctx(sessions: async_sessionmaker[AsyncSession], storage: FakeObjectStorage) -> dict[str, Any]:
    """The worker context the job would receive, minus arq."""
    return {
        "session_factory": sessions,
        "kms": kms_for_tests(),
        "object_storage": storage,
        "settings": settings,
    }


async def _write_audit_entries(sessions: async_sessionmaker[AsyncSession], count: int) -> None:
    """Append ``count`` real audit entries through the production write path.

    Note what is deliberately NOT done here: the entries are not backdated to get them behind the
    cutter's watermark. ``occurred_at`` is part of the audit preimage (ADR-0003 §2), so an
    ``UPDATE`` moving it changes every ``entry_hash`` and the ledger then legitimately fails
    verification — a test doing that would be measuring its own corruption. Tests that need the
    cut itself pass ``watermark_minutes=0`` instead; the watermark has its own dedicated tests.
    """
    for index in range(count):
        async with sessions() as session:
            await record_audit_event(
                session,
                kms=kms_for_tests(),
                actor_user_id=uuid.uuid4(),
                actor_role="investigator",
                action=f"action.{index}",
                module="ingestion",
                details={"i": index},
            )
            await session.commit()


async def _anchor_count(sessions: async_sessionmaker[AsyncSession], ledger: str) -> int:
    async with sessions() as session:
        result = await session.execute(
            select(func.count()).select_from(LedgerAnchor).where(LedgerAnchor.ledger == ledger)
        )
        return int(result.scalar_one())


# ---------------------------------------------------------------------------------------
# Empty ranges — what makes the job safe to put on a schedule
# ---------------------------------------------------------------------------------------


async def test_an_empty_ledger_cuts_nothing_and_does_not_raise(
    sessions: async_sessionmaker[AsyncSession], storage: FakeObjectStorage
) -> None:
    """A brand-new deployment runs this job every 4 hours before any evidence exists."""
    await cut_anchor_batches(_ctx(sessions, storage))

    assert await _anchor_count(sessions, LEDGER_AUDIT) == 0
    assert await _anchor_count(sessions, LEDGER_CUSTODY) == 0
    assert storage.retentions == {}


async def test_a_second_run_over_an_already_anchored_ledger_is_a_no_op(
    sessions: async_sessionmaker[AsyncSession], storage: FakeObjectStorage
) -> None:
    """Idempotence, and the property that lets arq retry a run without consequence.

    Re-anchoring the same range would create two commitments to the same entries, and a verifier
    meeting both would have no way to say which is authoritative.
    """
    await _write_audit_entries(sessions, 4)

    await cut_anchor_batches(_ctx(sessions, storage), watermark_minutes=0)
    assert await _anchor_count(sessions, LEDGER_AUDIT) == 1
    written_after_first = dict(storage.retentions)

    await cut_anchor_batches(_ctx(sessions, storage), watermark_minutes=0)
    assert await _anchor_count(sessions, LEDGER_AUDIT) == 1
    assert dict(storage.retentions) == written_after_first


async def test_entries_newer_than_the_watermark_are_not_anchored(
    sessions: async_sessionmaker[AsyncSession], storage: FakeObjectStorage
) -> None:
    """The guard against clock skew reordering an entry into a committed range.

    Without it, a write whose clock ran behind could be committed after a cut but sort before its
    boundary, changing the recomputed root for a ledger nobody touched — a false tampering alarm on
    healthy data.
    """
    await _write_audit_entries(sessions, 3)  # all written "now", inside the watermark

    await cut_anchor_batches(_ctx(sessions, storage))

    assert await _anchor_count(sessions, LEDGER_AUDIT) == 0


async def test_a_zero_watermark_anchors_right_up_to_the_present(
    sessions: async_sessionmaker[AsyncSession], storage: FakeObjectStorage
) -> None:
    """The control for the test above: the entries were anchorable, the watermark held them back."""
    await _write_audit_entries(sessions, 3)

    await cut_anchor_batches(_ctx(sessions, storage), watermark_minutes=0)

    assert await _anchor_count(sessions, LEDGER_AUDIT) == 1


# ---------------------------------------------------------------------------------------
# Cutting, and the end-to-end proof that the cut is verifiable
# ---------------------------------------------------------------------------------------


async def test_a_cut_anchor_covers_the_whole_chain_and_verifies(
    sessions: async_sessionmaker[AsyncSession], storage: FakeObjectStorage
) -> None:
    """The assertion the whole increment exists for.

    Not "a row was written" — that the Verification Engine reads the anchor back and reports the
    ledger verified, with zero unanchored entries. Before this job existed, that report said
    `unanchored_entries == entry_count` on every healthy ledger.
    """
    await _write_audit_entries(sessions, 5)

    await cut_anchor_batches(_ctx(sessions, storage), watermark_minutes=0)

    async with sessions() as session:
        anchors = await read_anchor_views(session, LEDGER_AUDIT)
        report = await AuditLedgerVerificationService(
            session, LedgerSigner(kms_for_tests())
        ).verify()

    assert len(anchors) == 1
    assert anchors[0].entry_count == 5
    assert report.state is VerificationState.VERIFIED
    assert report.anchors[0].state is VerificationState.VERIFIED
    assert report.unanchored_entries == 0


async def test_the_anchor_object_is_written_to_worm_under_a_retention_lock(
    sessions: async_sessionmaker[AsyncSession], storage: FakeObjectStorage
) -> None:
    """An anchor in an ordinary object is deletable, which would make it prove nothing."""
    await _write_audit_entries(sessions, 3)

    await cut_anchor_batches(_ctx(sessions, storage), watermark_minutes=0)

    async with sessions() as session:
        anchor = (await read_anchor_views(session, LEDGER_AUDIT))[0]

    bucket = settings.storage_anchor_bucket
    assert await storage.exists(bucket, anchor.worm_object_ref)
    retain_until = storage.retentions[(bucket, anchor.worm_object_ref)]
    # Retention has to outlive the evidence; the configured default is a decade.
    assert retain_until > datetime.now(UTC) + timedelta(days=365 * 9)


async def test_a_second_cut_anchors_only_the_new_tail(
    sessions: async_sessionmaker[AsyncSession], storage: FakeObjectStorage
) -> None:
    """Anchors are contiguous, forward-only intervals — never overlapping commitments."""
    await _write_audit_entries(sessions, 3)
    await cut_anchor_batches(_ctx(sessions, storage), watermark_minutes=0)

    await _write_audit_entries(sessions, 2)
    await cut_anchor_batches(_ctx(sessions, storage), watermark_minutes=0)

    async with sessions() as session:
        anchors = await read_anchor_views(session, LEDGER_AUDIT)
        report = await AuditLedgerVerificationService(
            session, LedgerSigner(kms_for_tests())
        ).verify()

    assert len(anchors) == 2
    assert sorted(a.entry_count for a in anchors) == [2, 3]
    # Both intervals still reconcile against the chain, and nothing is double-covered.
    assert report.state is VerificationState.VERIFIED
    assert report.unanchored_entries == 0


async def test_truncation_after_a_real_cut_is_detected(
    sessions: async_sessionmaker[AsyncSession], storage: FakeObjectStorage
) -> None:
    """The payoff: anchors cut by the scheduled job actually catch a deleted tail.

    Every previous truncation test in this repository published its anchor by hand. This one proves
    the *job's* output is load-bearing.
    """
    await _write_audit_entries(sessions, 6)
    await cut_anchor_batches(_ctx(sessions, storage), watermark_minutes=0)

    async with sessions() as session:
        await session.execute(
            text("DROP TRIGGER IF EXISTS audit_log_append_only ON platform.audit_log")
        )
        rows = (
            (await session.execute(select(AuditLog).order_by(AuditLog.occurred_at))).scalars().all()
        )
        for row in rows[4:]:
            await session.execute(
                text("DELETE FROM platform.audit_log WHERE audit_id = :i"), {"i": row.audit_id}
            )
        await session.commit()

    async with sessions() as session:
        report = await AuditLedgerVerificationService(
            session, LedgerSigner(kms_for_tests())
        ).verify()

    assert report.state is VerificationState.FAILED
    assert Finding.ANCHOR_RANGE_MISSING in report.anchors[0].findings


async def test_the_cutter_does_not_re_anchor_a_truncated_ledger(
    sessions: async_sessionmaker[AsyncSession], storage: FakeObjectStorage
) -> None:
    """A cutter that re-anchored from the start would destroy the evidence of the truncation.

    The old anchor's endpoint is gone, so its position cannot be resolved. Treating that as "nothing
    is anchored" would publish a fresh commitment over the doctored history — laundering the attack
    into a valid-looking proof. The tail is skipped instead, and the failing anchor stays on record.
    """
    await _write_audit_entries(sessions, 5)
    await cut_anchor_batches(_ctx(sessions, storage), watermark_minutes=0)

    async with sessions() as session:
        await session.execute(
            text("DROP TRIGGER IF EXISTS audit_log_append_only ON platform.audit_log")
        )
        rows = (
            (await session.execute(select(AuditLog).order_by(AuditLog.occurred_at))).scalars().all()
        )
        for row in rows[3:]:
            await session.execute(
                text("DELETE FROM platform.audit_log WHERE audit_id = :i"), {"i": row.audit_id}
            )
        await session.commit()

    await cut_anchor_batches(_ctx(sessions, storage), watermark_minutes=0)

    # Still exactly the original anchor: no new commitment was published over the survivors.
    assert await _anchor_count(sessions, LEDGER_AUDIT) == 1
    async with sessions() as session:
        report = await AuditLedgerVerificationService(
            session, LedgerSigner(kms_for_tests())
        ).verify()
    assert report.state is VerificationState.FAILED


# ---------------------------------------------------------------------------------------
# The boundary calculation, in isolation
# ---------------------------------------------------------------------------------------


def _anchor(last: str, *, first: str = "x" * 64, count: int = 1) -> AnchorView:
    return AnchorView(
        anchor_id=uuid.uuid4(),
        ledger=LEDGER_AUDIT,
        merkle_root="r" * 64,
        first_entry_hash=first,
        last_entry_hash=last,
        entry_count=count,
        signature_envelope=b"sig",
        worm_object_ref="anchors/x.json",
    )


def test_the_tail_of_an_unanchored_chain_is_the_whole_chain() -> None:
    assert _unanchored_tail(["a", "b", "c"], []) == ["a", "b", "c"]


def test_the_boundary_is_the_furthest_anchor_not_the_last_listed() -> None:
    """Order-independence matters: two anchors cut concurrently must not move the boundary back."""
    chain = ["a", "b", "c", "d", "e"]
    assert _unanchored_tail(chain, [_anchor("d"), _anchor("b")]) == ["e"]
    assert _unanchored_tail(chain, [_anchor("b"), _anchor("d")]) == ["e"]


def test_a_fully_anchored_chain_has_an_empty_tail() -> None:
    assert _unanchored_tail(["a", "b"], [_anchor("b")]) == []


def test_an_unresolvable_anchor_endpoint_refuses_the_cut_entirely() -> None:
    """``None`` means refuse, and it is not interchangeable with ``[]``.

    An anchor whose end is missing from the chain means entries it committed to are gone. Falling
    back to "nothing is anchored" would publish a fresh, correctly-signed commitment over the
    surviving history — laundering a truncation into proof. One unresolvable anchor stops the whole
    ledger's cut, even when another anchor resolves fine.
    """
    chain = ["a", "b", "c"]
    assert _unanchored_tail(chain, [_anchor("zzz")]) is None
    assert _unanchored_tail(chain, [_anchor("zzz"), _anchor("a")]) is None
    assert _unanchored_tail(chain, [_anchor("a"), _anchor("zzz")]) is None


def test_a_batch_is_capped_so_one_run_cannot_anchor_everything_forever() -> None:
    chain = [f"h{i}" for i in range(MAX_ENTRIES_PER_ANCHOR + 50)]
    assert len(_unanchored_tail(chain, [])) == MAX_ENTRIES_PER_ANCHOR


# ---------------------------------------------------------------------------------------
# RFC 3161 through the database — ADR-0003 §3, Wave 1.3c.
#
# The unit tests prove the token verifier and the anchor-service integration. What only a real
# database can prove is that the token survives `tsa_token_ref` — a `Text` column holding base64 of
# binary DER — and comes back byte-identical. A base64 slip there would produce anchors whose
# timestamps fail verification for a reason that has nothing to do with the timestamp.
# ---------------------------------------------------------------------------------------


class _DbFakeAuthority:
    """A ``TimestampAuthority`` over the in-process TSA, for the cutter's context."""

    def __init__(self, tsa: FakeTsa) -> None:
        self._tsa = tsa

    async def timestamp(self, message: bytes) -> tuple[bytes, VerifiedTimestamp]:
        return self._tsa.token_for(message), VerifiedTimestamp(
            gen_time=datetime.now(UTC),
            serial_number=7,
            hash_algo="sha256",
            signer_subject="CN=SentinelAI Test TSA",
            policy=None,
        )


async def _cut_with_tsa(
    sessions: async_sessionmaker[AsyncSession], storage: FakeObjectStorage, tsa: FakeTsa
) -> None:
    """Run one cut with timestamping wired in, bypassing `build_timestamp_authority`.

    The factory builds an HTTP client, and a test that exercised it would need a listening socket.
    What matters here is the persistence path, so the authority is injected directly.
    """
    service = LedgerAnchorService(
        kms_for_tests(),
        storage,
        bucket=settings.storage_anchor_bucket,
        timestamp_authority=_DbFakeAuthority(tsa),
    )
    async with sessions() as session:
        chain = await read_audit_chain_hashes(session)
        anchor = await service.publish(AnchorBatch(ledger=LEDGER_AUDIT, entry_hashes=tuple(chain)))
        session.add(
            LedgerAnchor(
                anchor_id=anchor.anchor_id,
                ledger=anchor.ledger,
                merkle_root=anchor.merkle_root,
                merkle_hash_algo=anchor.merkle_hash_algo,
                first_entry_hash=anchor.first_entry_hash,
                last_entry_hash=anchor.last_entry_hash,
                entry_count=anchor.entry_count,
                created_at=anchor.created_at,
                signature=anchor.signature.envelope,
                sig_alg=anchor.signature.sig_alg,
                key_id=anchor.signature.key_id,
                worm_object_ref=anchor.worm_object_ref,
                tsa_token_ref=(
                    base64.b64encode(anchor.tsa_token).decode("ascii")
                    if anchor.tsa_token is not None
                    else None
                ),
            )
        )
        await session.commit()


async def test_a_timestamped_anchor_round_trips_through_the_database_and_verifies(
    sessions: async_sessionmaker[AsyncSession], storage: FakeObjectStorage
) -> None:
    """The persisted path: token in, base64 through a Text column, token out, verified."""
    await _write_audit_entries(sessions, 4)
    tsa = FakeTsa()
    await _cut_with_tsa(sessions, storage, tsa)

    async with sessions() as session:
        stored = (await session.execute(select(LedgerAnchor))).scalars().one()
        assert stored.tsa_token_ref, "the token must be persisted, not dropped"

        report = await AuditLedgerVerificationService(
            session, LedgerSigner(kms_for_tests()), tsa_trust_anchors=tsa.trust_anchors
        ).verify()

    assert report.state is VerificationState.VERIFIED
    assert report.anchors[0].timestamped is True
    assert report.anchors[0].tsa_gen_time is not None
    assert report.untimestamped_anchors == 0


async def test_the_persisted_token_is_byte_identical_to_what_was_issued(
    sessions: async_sessionmaker[AsyncSession], storage: FakeObjectStorage
) -> None:
    """Base64 in a Text column is the only lossy-looking step in the chain; pin it."""
    await _write_audit_entries(sessions, 2)
    tsa = FakeTsa()
    await _cut_with_tsa(sessions, storage, tsa)

    async with sessions() as session:
        stored = (await session.execute(select(LedgerAnchor))).scalars().one()
        anchors = await read_anchor_views(session, LEDGER_AUDIT)

    assert anchors[0].tsa_token == base64.b64decode(stored.tsa_token_ref or "")
    # And it still verifies against the root, straight out of the database.
    verified = verify_timestamp_token(
        anchors[0].tsa_token or b"",
        message=anchors[0].merkle_root.encode("ascii"),
        trust_anchors=tsa.trust_anchors,
    )
    assert verified.signer_subject == "CN=SentinelAI Test TSA"


async def test_an_anchor_cut_without_a_tsa_persists_a_null_token(
    sessions: async_sessionmaker[AsyncSession], storage: FakeObjectStorage
) -> None:
    """The air-gapped path through the real cutter: NULL, and the ledger still verifies.

    NULL must be distinguishable from a stored-but-broken token — the verifier reports the first as
    untimestamped and the second as failed, and conflating them would hide a forgery.
    """
    await _write_audit_entries(sessions, 3)

    await cut_anchor_batches(_ctx(sessions, storage), watermark_minutes=0)

    async with sessions() as session:
        stored = (await session.execute(select(LedgerAnchor))).scalars().one()
        assert stored.tsa_token_ref is None
        report = await AuditLedgerVerificationService(
            session, LedgerSigner(kms_for_tests())
        ).verify()

    assert report.state is VerificationState.VERIFIED
    assert report.anchors[0].timestamped is False
    assert report.untimestamped_anchors == 1


async def test_a_persisted_token_from_an_untrusted_tsa_fails_verification(
    sessions: async_sessionmaker[AsyncSession], storage: FakeObjectStorage
) -> None:
    """A stored token verified against the wrong roots must fail, not be silently skipped."""
    await _write_audit_entries(sessions, 3)
    issuer = FakeTsa()
    await _cut_with_tsa(sessions, storage, issuer)

    async with sessions() as session:
        report = await AuditLedgerVerificationService(
            session, LedgerSigner(kms_for_tests()), tsa_trust_anchors=FakeTsa().trust_anchors
        ).verify()

    assert report.state is VerificationState.FAILED
    assert Finding.TSA_TOKEN_INVALID in report.anchors[0].findings
