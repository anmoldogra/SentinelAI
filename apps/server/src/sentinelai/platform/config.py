"""Application configuration (pydantic-settings).

Single source of typed settings, loaded from environment / ``.env`` (see
``.env.example``). In real environments these values arrive from Vault via the
External Secrets Operator, never a committed file (docs/deployment-architecture.md).
"""

from __future__ import annotations

from contextvars import ContextVar
from functools import lru_cache
from uuid import UUID

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# The five canonical deployment profiles (implementation-wave-1.md §5). No sixth profile exists.
VALID_PROFILES = frozenset({"development", "testing", "production", "air-gapped", "classified"})
# Production-grade profiles share every fail-closed rule; air-gapped/classified are overlays on it.
_PRODUCTION_GRADE = frozenset({"production", "air-gapped", "classified"})
_ZERO_EGRESS = frozenset({"air-gapped", "classified"})
# KMS providers acceptable for a classified deployment (HSM / managed KMS — never software `dev`).
_CLASSIFIED_KMS = frozenset({"vault_transit", "pkcs11", "aws_kms", "azure_key_vault", "gcp_kms"})
# Well-known placeholder secret values that must never reach a production-grade profile.
_PLACEHOLDER_SECRETS = frozenset(
    {"", "dev-only-change-me", "changeme", "change-me", "minioadmin", "root", "dev-only-token"}
)


class ConfigurationError(Exception):
    """A configuration invariant was violated for the active profile — fail closed.

    A startup/configuration failure (not an HTTP domain error — those live in
    ``sentinelai.shared.exceptions`` per guide Part 11). Raised only by
    :meth:`Settings.validate_for_profile` at entrypoint startup, before any connection is opened.
    Messages name the offending field, never the secret value.
    """


class Settings(BaseSettings):
    """Typed application settings. Field names map to UPPER_SNAKE env vars."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- application ---
    app_env: str = "development"
    log_level: str = "INFO"
    api_host: str = "0.0.0.0"
    api_port: int = 8000

    # --- database (async driver only) ---
    database_url: str = "postgresql+asyncpg://sentinelai:sentinelai@localhost:5432/sentinelai"
    db_pool_size: int = 10
    db_max_overflow: int = 5

    # --- redis (arq queue + caching) ---
    redis_url: str = "redis://localhost:6379/0"

    # --- auth / tokens ---
    # RESERVED, NOT ACTIVE. ADR-0010 mandates OPAQUE server-side sessions (not stateless JWT) for
    # authorization, with the store holding only a token hash (argon2id / keyed HMAC via ADR-0009).
    # These fields are retained for backward compatibility only; nothing authorizes on them, so
    # validate_for_profile() intentionally does NOT enforce them. Do not build auth on these.
    jwt_secret_key: SecretStr = SecretStr("dev-only-change-me")
    jwt_algorithm: str = "HS256"
    access_token_ttl_seconds: int = 900
    refresh_token_ttl_seconds: int = 1_209_600

    # ACTIVE. Absolute lifetime of an opaque session (security-architecture.md §9), deliberately
    # deployment-configurable — §9 expects government/high-sensitivity profiles to default
    # shorter than enterprise. This is the absolute cap only; the idle timeout §9 also calls for
    # arrives with the sliding-expiry refresh flow (POST /api/v1/auth/refresh), which is not
    # built yet — so a session currently lives exactly this long regardless of activity.
    session_ttl_seconds: int = 28_800  # 8h — one working shift.

    # --- object storage (evidence blobs) ---
    storage_endpoint_url: str = "http://localhost:9000"
    storage_bucket: str = "sentinelai-evidence"
    # Uploads land here first and are never served (ADR-0008 §2). Promotion into
    # ``storage_bucket`` happens only after a clean scan — a later increment.
    storage_quarantine_bucket: str = "sentinelai-quarantine"
    storage_access_key: SecretStr = SecretStr("minioadmin")
    storage_secret_key: SecretStr = SecretStr("minioadmin")
    # SigV4 requires a region even on MinIO, which ignores its value (ADR-0008).
    storage_region: str = "us-east-1"

    # --- notification delivery (security-architecture §25) ---
    # log (Phase 1: the in-app notification row is the durable delivery) | smtp | slack, later.
    notification_sender_provider: str = "log"

    # --- malware scanning (security-architecture §25) ---
    # dummy (dev/test only) | clamav
    malware_scanner_provider: str = "dummy"
    # clamd endpoint: a UNIX socket path takes precedence over host/port when set.
    clamav_host: str = "localhost"
    clamav_port: int = 3310
    clamav_socket_path: str | None = None
    clamav_timeout_seconds: float = 300.0
    # §25: a scanner running stale signatures gives false confidence. Beyond this age the
    # scanner reports DEGRADED health.
    clamav_max_signature_age_hours: int = 48

    # --- key management (KMS) — ADR-0009. Provider-agnostic; selected by config. ---
    kms_provider: str = "dev"  # dev | vault_transit | aws_kms | azure_key_vault | gcp_kms | pkcs11
    kms_dev_keystore: str = ".kms-dev-keystore"  # dev provider only; FORBIDDEN in production
    kms_signing_algorithm: str = "ED25519"  # policy default; callers never name an algorithm
    kms_hybrid_signing: bool = False  # policy may return a multi-signature bundle (PQC hybrid)
    # Vault Transit provider (only read when kms_provider == "vault_transit")
    vault_addr: str = "http://localhost:8200"
    vault_transit_mount: str = "transit"
    vault_token: SecretStr = SecretStr("dev-only-change-me")
    vault_namespace: str | None = None
    vault_auth_method: str = "token"  # token | approle
    vault_role_id: str | None = None
    vault_secret_id: SecretStr | None = None
    # KMS resilience (ADR-0009 H2)
    kms_retry_max_attempts: int = 4
    kms_retry_base_delay: float = 0.1
    kms_retry_max_delay: float = 5.0
    kms_timeout_seconds: float = 10.0
    kms_breaker_failure_threshold: int = 5
    kms_breaker_reset_seconds: float = 30.0

    @field_validator("app_env")
    @classmethod
    def _normalize_profile(cls, value: str) -> str:
        """Normalize + validate the deployment profile at construction (fail closed on unknown)."""
        normalized = value.strip().lower()
        if normalized == "prod":  # historical alias
            normalized = "production"
        if normalized not in VALID_PROFILES:
            raise ValueError(f"APP_ENV must be one of {sorted(VALID_PROFILES)}, got '{value}'")
        return normalized

    @property
    def is_production(self) -> bool:
        """True for every production-grade profile (production, air-gapped, classified).

        air-gapped and classified are hardening overlays on production, so all production
        fail-closed rules (no `dev` KMS, no placeholder secrets, etc.) apply to them too.
        """
        return self.app_env in _PRODUCTION_GRADE

    @property
    def is_air_gapped(self) -> bool:
        """True when the deployment must have zero configured egress (air-gapped or classified)."""
        return self.app_env in _ZERO_EGRESS

    @property
    def is_classified(self) -> bool:
        """True for the strictest profile (HSM-backed, no debug surface)."""
        return self.app_env == "classified"

    def validate_for_profile(self) -> None:
        """Assert profile-specific invariants; raise ConfigurationError on any violation.

        Called at entrypoint startup BEFORE any connection is opened so a misconfigured process
        fails to start rather than running degraded (implementation-wave-1.md §5). Development and
        testing are permissive; production-grade profiles fail closed. Error messages name the
        offending field only — never the secret value.
        """
        if not self.is_production:
            return

        problems: list[str] = []

        if self.kms_provider == "dev":
            problems.append("KMS_PROVIDER must not be 'dev' in a production-grade profile")
        elif self.is_classified and self.kms_provider not in _CLASSIFIED_KMS:
            problems.append(
                f"KMS_PROVIDER must be HSM/managed for a classified deployment "
                f"(one of {sorted(_CLASSIFIED_KMS)})"
            )

        if self.malware_scanner_provider == "dummy":
            # A no-op scanner would silently satisfy §25's gate while scanning nothing.
            problems.append(
                "MALWARE_SCANNER_PROVIDER must not be 'dummy' in a production-grade profile"
            )

        if "+asyncpg" not in self.database_url:
            problems.append("DATABASE_URL must use the async driver (postgresql+asyncpg)")

        # Actively-used credentials must not be placeholders. (JWT fields are reserved/inactive
        # per ADR-0010 and are intentionally excluded.)
        for field_name, secret in (
            ("STORAGE_ACCESS_KEY", self.storage_access_key),
            ("STORAGE_SECRET_KEY", self.storage_secret_key),
        ):
            if secret.get_secret_value() in _PLACEHOLDER_SECRETS:
                problems.append(f"{field_name} is empty or a default/placeholder value")

        if problems:
            raise ConfigurationError(
                f"invalid configuration for profile '{self.app_env}': " + "; ".join(problems)
            )


@lru_cache
def get_settings() -> Settings:
    """Cached settings accessor (one instance per process)."""
    return Settings()


settings: Settings = get_settings()

# Reserved single-tenant context (security-architecture.md §40, guide Part 8
# "Tenant Context"). Phase 1 is single-tenant: this is ALWAYS None until the
# Phase 4 multi-tenancy ADR. The extension point exists without speculative impl.
tenant_id: ContextVar[UUID | None] = ContextVar("tenant_id", default=None)
