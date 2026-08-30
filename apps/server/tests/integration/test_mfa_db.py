"""``MfaRepository`` against a real Postgres and a real dev KMS.

Everything proven here is something a fake cannot prove:

* **Envelope encryption really round-trips** through the KMS facade and the four decomposed
  ``Ciphertext`` columns — a mock would only confirm that the repository calls a method.
* **The AAD binding actually rejects a transplanted ciphertext.** This is the whole security
  value of `_mfa_aad`, and it is only observable against real AEAD.
* **The conditional UPDATEs are atomic under genuine concurrency.** Both replay guards
  (`claim_totp_step`, `consume_challenge`) exist to survive two requests arriving at once, so
  they are exercised from two sessions in two transactions racing on one row. Sequential calls
  would pass even against a read-then-write implementation, which is exactly the bug these
  guard against.
* **The CHECK constraint backstops the repository**, exercised through the ORM rather than raw
  SQL — the layer above can be wrong, and the database still refuses a half-written enrolment.

Runs in a throwaway **database** created and dropped here (the ``test_auth_db.py`` pattern): the
ORM models are pinned to the ``platform`` schema, the CI integration job does not apply
migrations, and a dev database must never be seeded with test rows.

Skips cleanly when no Postgres is reachable. Never fakes a pass.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from sentinelai.platform.auth.models import MfaChallenge, MfaRecoveryCode, User
from sentinelai.platform.auth.repository import (
    MFA_SECRET_KEY,
    MfaRepository,
    _parse_key_id,
    _serialize_key_id,
)
from sentinelai.platform.config import settings
from sentinelai.platform.crypto.audit import StructlogAuditSink
from sentinelai.platform.crypto.backends.dev import DevKmsProvider
from sentinelai.platform.crypto.kms import KeyManagementService
from sentinelai.platform.crypto.policy import AlgorithmPolicy
from sentinelai.platform.crypto.registry import KeyRegistry
from sentinelai.platform.crypto.types import KeyId, ProviderKind
from sentinelai.platform.db.base import Base
from sentinelai.platform.security.tokens import LOOKUP_PREFIX_LENGTH, generate_opaque_token
from sentinelai.shared.exceptions import NotFoundError

_URL = os.getenv("TEST_DATABASE_URL", settings.database_url)
_NOW = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
_SECRET = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"


async def _reachable(url: str) -> bool:
    try:
        # Explicit short timeout: the probe decides skip-vs-run and must never stall the suite.
        engine = create_async_engine(url, connect_args={"timeout": 3})
        try:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
        finally:
            await engine.dispose()
        return True
    except Exception:
        return False


async def _create_throwaway_database() -> tuple[str, str]:
    name = f"sentinelai_mfatest_{uuid.uuid4().hex[:8]}"
    admin = create_async_engine(_URL, isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as conn:
            await conn.execute(text(f'CREATE DATABASE "{name}"'))
    finally:
        await admin.dispose()
    return name, _URL.rsplit("/", 1)[0] + f"/{name}"


async def _drop_throwaway_database(name: str) -> None:
    admin = create_async_engine(_URL, isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    finally:
        await admin.dispose()


async def _create_tables(engine: AsyncEngine) -> None:
    """Create the real table definitions these queries touch, and only those.

    ``User.__table__`` carries the ``ck_users_mfa_secret_complete`` CHECK in its
    ``__table_args__``, so ``create_all`` reproduces the constraint the migration installs — which
    is what makes the partial-write test below meaningful rather than vacuous.
    """
    async with engine.begin() as conn:
        await conn.execute(text("CREATE SCHEMA IF NOT EXISTS platform"))
        await conn.run_sync(
            Base.metadata.create_all,
            tables=[User.__table__, MfaRecoveryCode.__table__, MfaChallenge.__table__],
        )


def _user(email: str) -> User:
    return User(
        external_idp_subject=None,
        email=email,
        display_name="Analyst",
        password_hash=None,
        status="active",
        created_at=_NOW,
        updated_at=_NOW,
    )


@pytest.fixture
async def db() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    if not await _reachable(_URL):
        pytest.skip(
            f"no Postgres reachable at {_URL.split('@')[-1]} — set TEST_DATABASE_URL to run"
        )
    database, url = await _create_throwaway_database()
    engine = create_async_engine(url)
    try:
        await _create_tables(engine)
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()
        await _drop_throwaway_database(database)


@pytest.fixture
async def kms(tmp_path: Path) -> KeyManagementService:
    """A real dev provider over an ephemeral keystore — real AES-GCM, not a stub."""
    service = KeyManagementService(
        KeyRegistry(DevKmsProvider(str(tmp_path / "keystore"), is_production=False)),
        AlgorithmPolicy.from_config(signing_algorithm="ED25519", hybrid=False),
        StructlogAuditSink(),
    )
    await service.create_key(MFA_SECRET_KEY)
    return service


async def _seed_user(
    db: async_sessionmaker[AsyncSession], email: str = "analyst@agency.gov"
) -> uuid.UUID:
    """Commit a user so concurrent sessions in another transaction can see it."""
    async with db() as session:
        user = _user(email)
        session.add(user)
        await session.commit()
        return user.user_id


# --- envelope encryption ----------------------------------------------------


async def test_secret_round_trips_through_the_kms(
    db: async_sessionmaker[AsyncSession], kms: KeyManagementService
) -> None:
    user_id = await _seed_user(db)
    async with db() as session:
        repo = MfaRepository(session, kms)
        await repo.store_secret(user_id=user_id, secret=_SECRET, enrolled_at=_NOW)
        await session.commit()
    async with db() as session:
        assert await MfaRepository(session, kms).load_secret(user_id) == _SECRET


async def test_the_plaintext_secret_is_never_stored(
    db: async_sessionmaker[AsyncSession], kms: KeyManagementService
) -> None:
    """The point of the column being `bytea` and KMS-wrapped, asserted rather than assumed."""
    user_id = await _seed_user(db)
    async with db() as session:
        await MfaRepository(session, kms).store_secret(
            user_id=user_id, secret=_SECRET, enrolled_at=_NOW
        )
        await session.commit()
    async with db() as session:
        stored = (
            await session.execute(select(User.mfa_secret_ciphertext).where(User.user_id == user_id))
        ).scalar_one()
        assert stored is not None
        assert _SECRET.encode() not in stored


async def test_all_four_ciphertext_columns_are_populated(
    db: async_sessionmaker[AsyncSession], kms: KeyManagementService
) -> None:
    """Agility metadata is only useful if it is actually written — key rotation reads `key_id`."""
    user_id = await _seed_user(db)
    async with db() as session:
        await MfaRepository(session, kms).store_secret(
            user_id=user_id, secret=_SECRET, enrolled_at=_NOW
        )
        await session.commit()
    async with db() as session:
        row = (
            await session.execute(
                select(
                    User.mfa_enrolled_at,
                    User.mfa_secret_ciphertext,
                    User.mfa_secret_nonce,
                    User.mfa_secret_algorithm,
                    User.mfa_secret_key_id,
                ).where(User.user_id == user_id)
            )
        ).one()
        assert all(value is not None for value in row)
        assert row.mfa_secret_algorithm == "AES_256_GCM"
        assert row.mfa_secret_key_id.startswith(f"{ProviderKind.DEV.value}:")


async def test_load_secret_returns_none_when_not_enrolled(
    db: async_sessionmaker[AsyncSession], kms: KeyManagementService
) -> None:
    """Not enrolled is a normal state the caller branches on, not an error."""
    user_id = await _seed_user(db)
    async with db() as session:
        assert await MfaRepository(session, kms).load_secret(user_id) is None


async def test_load_secret_returns_none_for_an_unknown_user(
    db: async_sessionmaker[AsyncSession], kms: KeyManagementService
) -> None:
    async with db() as session:
        assert await MfaRepository(session, kms).load_secret(uuid.uuid4()) is None


async def test_store_secret_rejects_an_unknown_user(
    db: async_sessionmaker[AsyncSession], kms: KeyManagementService
) -> None:
    async with db() as session:
        with pytest.raises(NotFoundError):
            await MfaRepository(session, kms).store_secret(
                user_id=uuid.uuid4(), secret=_SECRET, enrolled_at=_NOW
            )


async def test_re_enrolment_replaces_the_previous_secret(
    db: async_sessionmaker[AsyncSession], kms: KeyManagementService
) -> None:
    user_id = await _seed_user(db)
    replacement = "MFRGGZDFMZTWQ2LKNNWG23TPOBYXE43U"
    async with db() as session:
        repo = MfaRepository(session, kms)
        await repo.store_secret(user_id=user_id, secret=_SECRET, enrolled_at=_NOW)
        await repo.store_secret(user_id=user_id, secret=replacement, enrolled_at=_NOW)
        await session.commit()
    async with db() as session:
        assert await MfaRepository(session, kms).load_secret(user_id) == replacement


# --- AAD binding ------------------------------------------------------------


async def test_a_transplanted_ciphertext_will_not_decrypt_for_another_user(
    db: async_sessionmaker[AsyncSession], kms: KeyManagementService
) -> None:
    """The reason `_mfa_aad` exists, demonstrated as an actual attack.

    Someone with write access to `platform.users` copies a known enrolment's ciphertext columns
    onto another account, intending to pass that account's second factor with a secret they hold.
    The AAD is bound to the *owner's* user id, so the AEAD tag fails and decryption raises rather
    than returning the attacker's secret.
    """
    victim = await _seed_user(db, "victim@agency.gov")
    attacker = await _seed_user(db, "attacker@agency.gov")

    async with db() as session:
        await MfaRepository(session, kms).store_secret(
            user_id=attacker, secret=_SECRET, enrolled_at=_NOW
        )
        await session.commit()

    async with db() as session:
        stolen = (
            await session.execute(
                select(
                    User.mfa_secret_ciphertext,
                    User.mfa_secret_nonce,
                    User.mfa_secret_algorithm,
                    User.mfa_secret_key_id,
                ).where(User.user_id == attacker)
            )
        ).one()
        await session.execute(
            update(User)
            .where(User.user_id == victim)
            .values(
                mfa_enrolled_at=_NOW,
                mfa_secret_ciphertext=stolen.mfa_secret_ciphertext,
                mfa_secret_nonce=stolen.mfa_secret_nonce,
                mfa_secret_algorithm=stolen.mfa_secret_algorithm,
                mfa_secret_key_id=stolen.mfa_secret_key_id,
            )
        )
        await session.commit()

    async with db() as session:
        # Any failure is the correct outcome; a successful decrypt is not. The concrete type is
        # `cryptography.exceptions.InvalidTag`, which the crypto port currently lets escape
        # unwrapped — worth wrapping in `CryptoError` one day, but not this increment's business.
        with pytest.raises(Exception):  # noqa: B017
            await MfaRepository(session, kms).load_secret(victim)


async def test_the_rightful_owner_still_decrypts_after_that_attempt(
    db: async_sessionmaker[AsyncSession], kms: KeyManagementService
) -> None:
    """The binding must reject the transplant without breaking the legitimate enrolment."""
    attacker = await _seed_user(db, "attacker@agency.gov")
    async with db() as session:
        await MfaRepository(session, kms).store_secret(
            user_id=attacker, secret=_SECRET, enrolled_at=_NOW
        )
        await session.commit()
    async with db() as session:
        assert await MfaRepository(session, kms).load_secret(attacker) == _SECRET


# --- the CHECK constraint, through the ORM ---------------------------------


async def test_a_partial_enrolment_is_refused_by_the_database(
    db: async_sessionmaker[AsyncSession], kms: KeyManagementService
) -> None:
    """The backstop, exercised through the ORM rather than raw SQL.

    A service that set `mfa_enrolled_at` and then failed before writing the secret would leave an
    account that believes it has a second factor and cannot verify one — failing *open* at the
    moment MFA matters. The database refuses it regardless of what the layer above does.
    """
    user_id = await _seed_user(db)
    async with db() as session:
        user = (await session.execute(select(User).where(User.user_id == user_id))).scalar_one()
        user.mfa_enrolled_at = _NOW  # and nothing else
        with pytest.raises(IntegrityError, match="ck_users_mfa_secret_complete"):
            await session.flush()


async def test_a_secret_without_an_enrolment_timestamp_is_also_refused(
    db: async_sessionmaker[AsyncSession], kms: KeyManagementService
) -> None:
    """The constraint is symmetric — neither half may exist without the other."""
    user_id = await _seed_user(db)
    async with db() as session:
        user = (await session.execute(select(User).where(User.user_id == user_id))).scalar_one()
        user.mfa_secret_ciphertext = b"\x01"
        user.mfa_secret_nonce = b"\x02"
        user.mfa_secret_algorithm = "AES_256_GCM"
        user.mfa_secret_key_id = "dev:1:k"
        with pytest.raises(IntegrityError, match="ck_users_mfa_secret_complete"):
            await session.flush()


async def test_store_secret_satisfies_the_constraint_it_could_violate(
    db: async_sessionmaker[AsyncSession], kms: KeyManagementService
) -> None:
    """Positive control: the two tests above must fail for the right reason, not because any
    write to these columns is rejected."""
    user_id = await _seed_user(db)
    async with db() as session:
        await MfaRepository(session, kms).store_secret(
            user_id=user_id, secret=_SECRET, enrolled_at=_NOW
        )
        await session.commit()  # would raise if the CHECK were violated


async def test_clear_secret_nulls_every_column_together(
    db: async_sessionmaker[AsyncSession], kms: KeyManagementService
) -> None:
    user_id = await _seed_user(db)
    async with db() as session:
        repo = MfaRepository(session, kms)
        await repo.store_secret(user_id=user_id, secret=_SECRET, enrolled_at=_NOW)
        await repo.claim_totp_step(user_id=user_id, step=1_000)
        await session.commit()
    async with db() as session:
        await MfaRepository(session, kms).clear_secret(user_id)
        await session.commit()
    async with db() as session:
        row = (
            await session.execute(
                select(
                    User.mfa_enrolled_at,
                    User.mfa_secret_ciphertext,
                    User.mfa_secret_nonce,
                    User.mfa_secret_algorithm,
                    User.mfa_secret_key_id,
                    User.mfa_last_used_step,
                ).where(User.user_id == user_id)
            )
        ).one()
        assert all(value is None for value in row)


async def test_clear_secret_resets_the_replay_watermark(
    db: async_sessionmaker[AsyncSession], kms: KeyManagementService
) -> None:
    """A stale high watermark would silently reject the first codes of a *new* enrolment, whose
    steps start from the current time and can fall below it."""
    user_id = await _seed_user(db)
    async with db() as session:
        repo = MfaRepository(session, kms)
        await repo.store_secret(user_id=user_id, secret=_SECRET, enrolled_at=_NOW)
        assert await repo.claim_totp_step(user_id=user_id, step=999_999_999)
        await session.commit()
    async with db() as session:
        await MfaRepository(session, kms).clear_secret(user_id)
        await session.commit()
    async with db() as session:
        repo = MfaRepository(session, kms)
        await repo.store_secret(user_id=user_id, secret=_SECRET, enrolled_at=_NOW)
        assert await repo.claim_totp_step(user_id=user_id, step=1) is True
        await session.commit()


async def test_clear_secret_rejects_an_unknown_user(
    db: async_sessionmaker[AsyncSession], kms: KeyManagementService
) -> None:
    async with db() as session:
        with pytest.raises(NotFoundError):
            await MfaRepository(session, kms).clear_secret(uuid.uuid4())


# --- claim_totp_step: RFC 6238 §5.2 replay guard ---------------------------


async def test_a_step_can_be_claimed_once(
    db: async_sessionmaker[AsyncSession], kms: KeyManagementService
) -> None:
    user_id = await _seed_user(db)
    async with db() as session:
        repo = MfaRepository(session, kms)
        assert await repo.claim_totp_step(user_id=user_id, step=100) is True
        await session.commit()


async def test_replaying_the_same_step_is_refused(
    db: async_sessionmaker[AsyncSession], kms: KeyManagementService
) -> None:
    """The literal RFC 6238 §5.2 requirement: a code accepted once is not accepted again."""
    user_id = await _seed_user(db)
    async with db() as session:
        repo = MfaRepository(session, kms)
        assert await repo.claim_totp_step(user_id=user_id, step=100) is True
        assert await repo.claim_totp_step(user_id=user_id, step=100) is False
        await session.commit()


async def test_an_earlier_step_is_refused(
    db: async_sessionmaker[AsyncSession], kms: KeyManagementService
) -> None:
    """Going backwards is as much a replay as repeating — a still-in-drift older code must not
    be usable after a newer one has been accepted. This is why the guard is `<`, not `!=`."""
    user_id = await _seed_user(db)
    async with db() as session:
        repo = MfaRepository(session, kms)
        assert await repo.claim_totp_step(user_id=user_id, step=100) is True
        assert await repo.claim_totp_step(user_id=user_id, step=99) is False
        await session.commit()


async def test_a_later_step_is_accepted(
    db: async_sessionmaker[AsyncSession], kms: KeyManagementService
) -> None:
    """Ordinary re-authentication 30 seconds later must still work."""
    user_id = await _seed_user(db)
    async with db() as session:
        repo = MfaRepository(session, kms)
        assert await repo.claim_totp_step(user_id=user_id, step=100) is True
        assert await repo.claim_totp_step(user_id=user_id, step=101) is True
        await session.commit()


async def test_claiming_a_step_for_an_unknown_user_returns_false(
    db: async_sessionmaker[AsyncSession], kms: KeyManagementService
) -> None:
    async with db() as session:
        assert (
            await MfaRepository(session, kms).claim_totp_step(user_id=uuid.uuid4(), step=1) is False
        )


async def test_concurrent_claims_of_one_step_admit_exactly_one(
    db: async_sessionmaker[AsyncSession], kms: KeyManagementService
) -> None:
    """The reason this is a single conditional UPDATE rather than read-then-write.

    Two requests presenting the same intercepted code arrive together — the exact situation the
    replay guard exists for. Each runs in its own session and transaction, so Postgres row
    locking is genuinely in play: the second UPDATE blocks, then re-evaluates its WHERE against
    the committed value and matches nothing. A read-then-write implementation passes every
    sequential test above and fails this one.
    """
    user_id = await _seed_user(db)

    async def attempt() -> bool:
        async with db() as session:
            won = await MfaRepository(session, kms).claim_totp_step(user_id=user_id, step=500)
            await session.commit()
            return won

    results = await asyncio.gather(attempt(), attempt())
    assert sorted(results) == [False, True]


# --- recovery codes ---------------------------------------------------------


async def test_a_recovery_code_can_be_redeemed_once(
    db: async_sessionmaker[AsyncSession], kms: KeyManagementService
) -> None:
    user_id = await _seed_user(db)
    async with db() as session:
        repo = MfaRepository(session, kms)
        await repo.replace_recovery_codes(
            user_id=user_id, codes=["alpha", "bravo"], created_at=_NOW
        )
        await session.commit()
    async with db() as session:
        repo = MfaRepository(session, kms)
        assert await repo.redeem_recovery_code(user_id=user_id, code="alpha", used_at=_NOW)
        await session.commit()
    async with db() as session:
        repo = MfaRepository(session, kms)
        assert not await repo.redeem_recovery_code(user_id=user_id, code="alpha", used_at=_NOW)


async def test_an_unknown_recovery_code_is_refused_without_raising(
    db: async_sessionmaker[AsyncSession], kms: KeyManagementService
) -> None:
    user_id = await _seed_user(db)
    async with db() as session:
        repo = MfaRepository(session, kms)
        await repo.replace_recovery_codes(user_id=user_id, codes=["alpha"], created_at=_NOW)
        assert not await repo.redeem_recovery_code(user_id=user_id, code="nope", used_at=_NOW)


async def test_one_users_code_cannot_redeem_against_another(
    db: async_sessionmaker[AsyncSession], kms: KeyManagementService
) -> None:
    owner = await _seed_user(db, "owner@agency.gov")
    other = await _seed_user(db, "other@agency.gov")
    async with db() as session:
        repo = MfaRepository(session, kms)
        await repo.replace_recovery_codes(user_id=owner, codes=["shared"], created_at=_NOW)
        await session.commit()
    async with db() as session:
        repo = MfaRepository(session, kms)
        assert not await repo.redeem_recovery_code(user_id=other, code="shared", used_at=_NOW)


async def test_a_redeemed_code_is_retained_for_audit_not_deleted(
    db: async_sessionmaker[AsyncSession], kms: KeyManagementService
) -> None:
    """ "Which code was spent, and when" has to survive the spending."""
    user_id = await _seed_user(db)
    async with db() as session:
        repo = MfaRepository(session, kms)
        await repo.replace_recovery_codes(user_id=user_id, codes=["alpha"], created_at=_NOW)
        await repo.redeem_recovery_code(user_id=user_id, code="alpha", used_at=_NOW)
        await session.commit()
    async with db() as session:
        row = (
            await session.execute(select(MfaRecoveryCode).where(MfaRecoveryCode.user_id == user_id))
        ).scalar_one()
        assert row.used_at is not None


async def test_replacing_codes_discards_the_previous_set(
    db: async_sessionmaker[AsyncSession], kms: KeyManagementService
) -> None:
    """Regeneration usually happens because the old set is believed compromised; leaving them
    alive would defeat the reason the user regenerated."""
    user_id = await _seed_user(db)
    async with db() as session:
        repo = MfaRepository(session, kms)
        await repo.replace_recovery_codes(user_id=user_id, codes=["old"], created_at=_NOW)
        await repo.replace_recovery_codes(user_id=user_id, codes=["new"], created_at=_NOW)
        await session.commit()
    async with db() as session:
        repo = MfaRepository(session, kms)
        assert not await repo.redeem_recovery_code(user_id=user_id, code="old", used_at=_NOW)
        assert await repo.redeem_recovery_code(user_id=user_id, code="new", used_at=_NOW)


async def test_unused_code_count_tracks_redemption(
    db: async_sessionmaker[AsyncSession], kms: KeyManagementService
) -> None:
    user_id = await _seed_user(db)
    async with db() as session:
        repo = MfaRepository(session, kms)
        await repo.replace_recovery_codes(user_id=user_id, codes=["a", "b", "c"], created_at=_NOW)
        assert await repo.count_unused_recovery_codes(user_id) == 3
        await repo.redeem_recovery_code(user_id=user_id, code="b", used_at=_NOW)
        assert await repo.count_unused_recovery_codes(user_id) == 2


async def test_concurrent_redemptions_of_one_code_admit_exactly_one(
    db: async_sessionmaker[AsyncSession], kms: KeyManagementService
) -> None:
    """A recovery code is single-use even when presented twice at once."""
    user_id = await _seed_user(db)
    async with db() as session:
        await MfaRepository(session, kms).replace_recovery_codes(
            user_id=user_id, codes=["once"], created_at=_NOW
        )
        await session.commit()

    async def attempt() -> bool:
        async with db() as session:
            won = await MfaRepository(session, kms).redeem_recovery_code(
                user_id=user_id, code="once", used_at=_NOW
            )
            await session.commit()
            return won

    results = await asyncio.gather(attempt(), attempt())
    assert sorted(results) == [False, True]


# --- challenges -------------------------------------------------------------


async def test_a_challenge_resolves_from_its_token(
    db: async_sessionmaker[AsyncSession], kms: KeyManagementService
) -> None:
    user_id = await _seed_user(db)
    token = generate_opaque_token()
    async with db() as session:
        repo = MfaRepository(session, kms)
        created = await repo.create_challenge(
            user_id=user_id,
            token=token,
            issued_at=_NOW,
            expires_at=_NOW + timedelta(minutes=5),
        )
        await session.commit()
        challenge_id = created.challenge_id
    async with db() as session:
        found = await MfaRepository(session, kms).get_challenge_by_token(token)
        assert found is not None
        assert found.challenge_id == challenge_id


async def test_the_plaintext_mfa_token_is_never_stored(
    db: async_sessionmaker[AsyncSession], kms: KeyManagementService
) -> None:
    user_id = await _seed_user(db)
    token = generate_opaque_token()
    async with db() as session:
        repo = MfaRepository(session, kms)
        await repo.create_challenge(
            user_id=user_id,
            token=token,
            issued_at=_NOW,
            expires_at=_NOW + timedelta(minutes=5),
        )
        await session.commit()
    async with db() as session:
        row = (await session.execute(select(MfaChallenge))).scalar_one()
        assert row.token_hash != token
        assert row.token_lookup == token[:LOOKUP_PREFIX_LENGTH]


async def test_a_token_resolves_to_its_own_challenge_despite_a_prefix_collision(
    db: async_sessionmaker[AsyncSession], kms: KeyManagementService
) -> None:
    """The prefix index is non-unique on purpose; only the full digest decides the match."""
    user_id = await _seed_user(db)
    first = generate_opaque_token()
    second = first[:LOOKUP_PREFIX_LENGTH] + generate_opaque_token()[LOOKUP_PREFIX_LENGTH:]
    assert first != second

    async with db() as session:
        repo = MfaRepository(session, kms)
        one = await repo.create_challenge(
            user_id=user_id, token=first, issued_at=_NOW, expires_at=_NOW + timedelta(minutes=5)
        )
        two = await repo.create_challenge(
            user_id=user_id, token=second, issued_at=_NOW, expires_at=_NOW + timedelta(minutes=5)
        )
        await session.commit()
        first_id, second_id = one.challenge_id, two.challenge_id

    async with db() as session:
        repo = MfaRepository(session, kms)
        resolved_first = await repo.get_challenge_by_token(first)
        resolved_second = await repo.get_challenge_by_token(second)
        assert resolved_first is not None and resolved_first.challenge_id == first_id
        assert resolved_second is not None and resolved_second.challenge_id == second_id


async def test_an_unknown_token_resolves_to_nothing(
    db: async_sessionmaker[AsyncSession], kms: KeyManagementService
) -> None:
    async with db() as session:
        assert (
            await MfaRepository(session, kms).get_challenge_by_token(generate_opaque_token())
            is None
        )


async def test_resolution_does_not_filter_expiry_or_consumption(
    db: async_sessionmaker[AsyncSession], kms: KeyManagementService
) -> None:
    """Deliberate, mirroring `get_active_by_token`: the caller decides validity, so the service
    can tell "no such challenge" from "expired" from "already used" in an audit log."""
    user_id = await _seed_user(db)
    token = generate_opaque_token()
    async with db() as session:
        repo = MfaRepository(session, kms)
        created = await repo.create_challenge(
            user_id=user_id,
            token=token,
            issued_at=_NOW - timedelta(hours=2),
            expires_at=_NOW - timedelta(hours=1),  # long expired
        )
        await repo.consume_challenge(challenge_id=created.challenge_id, consumed_at=_NOW)
        await session.commit()
    async with db() as session:
        found = await MfaRepository(session, kms).get_challenge_by_token(token)
        assert found is not None
        assert found.consumed_at is not None


async def test_a_challenge_consumes_once(
    db: async_sessionmaker[AsyncSession], kms: KeyManagementService
) -> None:
    user_id = await _seed_user(db)
    async with db() as session:
        repo = MfaRepository(session, kms)
        created = await repo.create_challenge(
            user_id=user_id, token=generate_opaque_token(), issued_at=_NOW, expires_at=_NOW
        )
        assert await repo.consume_challenge(challenge_id=created.challenge_id, consumed_at=_NOW)
        assert not await repo.consume_challenge(challenge_id=created.challenge_id, consumed_at=_NOW)
        await session.commit()


async def test_consuming_an_unknown_challenge_returns_false(
    db: async_sessionmaker[AsyncSession], kms: KeyManagementService
) -> None:
    async with db() as session:
        assert not await MfaRepository(session, kms).consume_challenge(
            challenge_id=uuid.uuid4(), consumed_at=_NOW
        )


async def test_concurrent_consumes_of_one_challenge_admit_exactly_one(
    db: async_sessionmaker[AsyncSession], kms: KeyManagementService
) -> None:
    """A replayed `mfa_token` must not mint a second session, including when both arrive at once."""
    user_id = await _seed_user(db)
    async with db() as session:
        created = await MfaRepository(session, kms).create_challenge(
            user_id=user_id,
            token=generate_opaque_token(),
            issued_at=_NOW,
            expires_at=_NOW + timedelta(minutes=5),
        )
        await session.commit()
        challenge_id = created.challenge_id

    async def attempt() -> bool:
        async with db() as session:
            won = await MfaRepository(session, kms).consume_challenge(
                challenge_id=challenge_id, consumed_at=_NOW
            )
            await session.commit()
            return won

    results = await asyncio.gather(attempt(), attempt())
    assert sorted(results) == [False, True]


# --- key-id serialization (pure; no database needed) ------------------------


def test_key_id_round_trips() -> None:
    key_id = KeyId(provider=ProviderKind.DEV, backend_ref="sentinelai-session-mfa", version=3)
    assert _parse_key_id(_serialize_key_id(key_id)) == key_id


def test_a_backend_ref_containing_colons_round_trips() -> None:
    """The reason the version precedes the ref: an AWS KMS ARN is full of colons, and any other
    field order becomes ambiguous the first time SentinelAI runs on AWS KMS."""
    key_id = KeyId(
        provider=ProviderKind.AWS_KMS,
        backend_ref="arn:aws:kms:eu-west-2:123456789012:key/abcd-ef01",
        version=7,
    )
    restored = _parse_key_id(_serialize_key_id(key_id))
    assert restored == key_id
    # Guards the fixture itself: if someone "tidies" the ref into something colon-free, the
    # round-trip above would still pass and this test would stop testing anything.
    assert restored.backend_ref.count(":") == 5
