"""Identity bootstrap CLI — create a user, grant a role, mint a development session.

`POST /api/v1/auth/login` cannot be used until at least one user exists, and nothing in the
codebase could create one: there is no `POST /api/v1/admin/users` yet, and `platform.roles` ships
empty (the initial migration creates the table but seeds no rows). This closes that gap without
adding an HTTP surface.

Run it as a module::

    python -m sentinelai.cli.admin create-user --email a@b.gov --role admin
    python -m sentinelai.cli.admin dev-token --email a@b.gov

or via the `create-admin` / `dev-token` Makefile targets.

**`dev-token` is a development affordance and refuses to run on a production-grade profile.** It
mints a long-lived session out of band and prints the plaintext bearer token to stdout, bypassing
password verification, MFA, and rate limiting — every reason those exist is a reason this must not
run against production. `create-user` has no such restriction; provisioning a real operator is a
legitimate action anywhere.

Both commands are idempotent: re-running `create-user` will not duplicate a user or a role grant.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import sys
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sentinelai.platform.auth.audit import record_audit_event
from sentinelai.platform.auth.models import Role, User, UserRole
from sentinelai.platform.auth.repository import SessionRepository, UserRepository
from sentinelai.platform.config import settings
from sentinelai.platform.db.session import async_session_factory, dispose_engine
from sentinelai.platform.security.hashing import Argon2PasswordHasher
from sentinelai.platform.security.tokens import generate_opaque_token

# The RBAC roles api-design.md §3 and security-architecture.md §6 define. Constrained on purpose:
# a typo'd role name would create a role that grants nothing and authorizes nowhere.
ROLE_NAMES = ("investigator", "supervisor", "admin", "system", "compliance")
_ROLE_DESCRIPTIONS = {
    "investigator": "Case and evidence work within granted case scope.",
    "supervisor": "Investigator scope plus review and approval authority.",
    "admin": "Platform administration: users, roles, configuration.",
    "system": "Connector and service accounts; non-human callers.",
    "compliance": "Audit and compliance review; read-oriented oversight.",
}
# Long enough that a developer is not re-minting a token mid-task; short enough that a forgotten
# one does not stay valid indefinitely.
_DEV_TOKEN_DEFAULT_DAYS = 30
_ENV_PASSWORD = "SENTINELAI_ADMIN_PASSWORD"


def _resolve_password(supplied: str | None) -> str:
    """Take the password from the flag, the environment, or an interactive prompt.

    The flag is the least private of the three — it lands in shell history and is visible in the
    process list — so it warns, and the env var and prompt exist so real provisioning need not
    use it.
    """
    if supplied is not None:
        print(
            f"warning: --password is visible in shell history and process listings; "
            f"prefer {_ENV_PASSWORD} or the interactive prompt outside dev.",
            file=sys.stderr,
        )
        return supplied

    from_env = os.environ.get(_ENV_PASSWORD)
    if from_env:
        return from_env

    if not sys.stdin.isatty():
        raise SystemExit(
            f"error: no password supplied. Pass --password, set {_ENV_PASSWORD}, "
            "or run interactively."
        )
    first = getpass.getpass("Password: ")
    if first != getpass.getpass("Confirm password: "):
        raise SystemExit("error: passwords did not match.")
    if not first:
        raise SystemExit("error: password must not be empty.")
    return first


async def _ensure_role(session: AsyncSession, name: str) -> Role:
    """Return the named role, creating it if the (unseeded) roles table has no such row."""
    existing = (await session.execute(select(Role).where(Role.name == name))).scalars().first()
    if existing is not None:
        return existing
    role = Role(name=name, description=_ROLE_DESCRIPTIONS[name])
    session.add(role)
    await session.flush()
    return role


async def _grant_role(session: AsyncSession, user_id: UUID, role: Role) -> bool:
    """Grant ``role`` to the user. Returns False when the grant already existed."""
    already = (
        (
            await session.execute(
                select(UserRole).where(
                    UserRole.user_id == user_id, UserRole.role_id == role.role_id
                )
            )
        )
        .scalars()
        .first()
    )
    if already is not None:
        return False
    session.add(UserRole(user_id=user_id, role_id=role.role_id, granted_at=datetime.now(UTC)))
    await session.flush()
    return True


async def create_user(
    *,
    email: str,
    password: str,
    role_name: str,
    display_name: str | None,
    update_password: bool,
) -> int:
    """Create (or top up) a user and ensure the role grant. Idempotent."""
    hasher = Argon2PasswordHasher()
    async with async_session_factory() as session:
        users = UserRepository(session)
        existing = await users.get_by_email(email)
        now = datetime.now(UTC)

        if existing is None:
            user = User(
                external_idp_subject=None,
                email=email,
                display_name=display_name or email.split("@")[0],
                password_hash=hasher.hash(password),
                status="active",
                created_at=now,
                updated_at=now,
            )
            session.add(user)
            await session.flush()
            print(f"created user {user.email} ({user.user_id})")
        else:
            user = existing
            if update_password:
                user.password_hash = hasher.hash(password)
                user.updated_at = now
                print(f"user {user.email} already existed — password updated")
            else:
                # Silently rewriting a password on a re-run would be a nasty surprise on a
                # shared database, so changing it has to be asked for.
                print(
                    f"user {user.email} already exists — password left unchanged "
                    "(pass --update-password to reset it)"
                )

        role = await _ensure_role(session, role_name)
        if await _grant_role(session, user.user_id, role):
            print(f"granted role '{role_name}'")
        else:
            print(f"role '{role_name}' already granted")

        await record_audit_event(
            session,
            actor_user_id=None,  # provisioned out of band, not by a signed-in operator
            actor_role="system",
            action="user_provisioned",
            module="platform",
            target_type="user",
            target_id=user.user_id,
            details={"email": user.email, "role": role_name, "via": "cli"},
        )
        await session.commit()  # ADR-0005: the entrypoint owns the transaction
    return 0


async def issue_dev_token(*, email: str, ttl_days: int, quiet: bool) -> int:
    """Mint a session for an existing user and print its plaintext bearer token."""
    if settings.is_production:
        raise SystemExit(
            f"error: refusing to mint a development token on a production-grade profile "
            f"(APP_ENV={settings.app_env}). This bypasses password verification and MFA."
        )

    async with async_session_factory() as session:
        user = await UserRepository(session).get_by_email(email)
        if user is None:
            raise SystemExit(f"error: no user with email {email!r}. Run `make create-admin` first.")

        token = generate_opaque_token()
        issued_at = datetime.now(UTC)
        # Reuses the login path's own writer, so the digest and lookup prefix are produced
        # exactly as `POST /auth/login` produces them — a token minted here and one issued by
        # login cannot drift apart.
        row = await SessionRepository(session).create_session(
            user_id=user.user_id,
            token=token,
            issued_at=issued_at,
            expires_at=issued_at + timedelta(days=ttl_days),
        )
        await record_audit_event(
            session,
            actor_user_id=user.user_id,
            actor_role="system",
            action="dev_session_issued",
            module="platform",
            target_type="session",
            target_id=row.session_id,
            details={"ttl_days": ttl_days, "via": "cli"},
        )
        await session.commit()  # ADR-0005: the entrypoint owns the transaction

    if quiet:
        print(token)
        return 0

    print(f"\nsession {row.session_id} for {user.email}, expires {row.expires_at.isoformat()}")
    print("\nPaste into apps/web/.env.local (gitignored):\n")
    print(f"VITE_DEV_ACCESS_TOKEN={token}\n")
    print("The plaintext token is shown once — only its hash is stored (ADR-0010 §1).")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m sentinelai.cli.admin",
        description="Bootstrap identities for local development and initial provisioning.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser("create-user", help="Create a user and grant a role (idempotent)")
    create.add_argument("--email", required=True)
    create.add_argument(
        "--password",
        help=f"Dev convenience. Prefer {_ENV_PASSWORD} or the interactive prompt.",
    )
    create.add_argument("--role", required=True, choices=ROLE_NAMES)
    create.add_argument("--display-name", help="Defaults to the local part of the email.")
    create.add_argument(
        "--update-password",
        action="store_true",
        help="Reset the password if the user already exists.",
    )

    token = sub.add_parser("dev-token", help="Mint a long-lived session token (non-production)")
    token.add_argument("--email", required=True)
    token.add_argument("--ttl-days", type=int, default=_DEV_TOKEN_DEFAULT_DAYS)
    token.add_argument(
        "--quiet", action="store_true", help="Print only the token, for shell capture."
    )

    return parser


async def _run(args: argparse.Namespace) -> int:
    # Same fail-closed check both other entrypoints make before opening a connection.
    settings.validate_for_profile()
    try:
        if args.command == "create-user":
            return await create_user(
                email=args.email,
                password=_resolve_password(args.password),
                role_name=args.role,
                display_name=args.display_name,
                update_password=args.update_password,
            )
        return await issue_dev_token(email=args.email, ttl_days=args.ttl_days, quiet=args.quiet)
    finally:
        await dispose_engine()


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
