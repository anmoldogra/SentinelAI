"""Application configuration (pydantic-settings).

Single source of typed settings, loaded from environment / ``.env`` (see
``.env.example``). In real environments these values arrive from Vault via the
External Secrets Operator, never a committed file (docs/deployment-architecture.md).
"""

from __future__ import annotations

from contextvars import ContextVar
from functools import lru_cache
from uuid import UUID

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


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
    jwt_secret_key: SecretStr = SecretStr("dev-only-change-me")
    jwt_algorithm: str = "HS256"
    access_token_ttl_seconds: int = 900
    refresh_token_ttl_seconds: int = 1_209_600

    # --- object storage (evidence blobs) ---
    storage_endpoint_url: str = "http://localhost:9000"
    storage_bucket: str = "sentinelai-evidence"
    storage_access_key: SecretStr = SecretStr("minioadmin")
    storage_secret_key: SecretStr = SecretStr("minioadmin")

    @property
    def is_production(self) -> bool:
        return self.app_env.lower() in {"production", "prod"}


@lru_cache
def get_settings() -> Settings:
    """Cached settings accessor (one instance per process)."""
    return Settings()


settings: Settings = get_settings()

# Reserved single-tenant context (security-architecture.md §40, guide Part 8
# "Tenant Context"). Phase 1 is single-tenant: this is ALWAYS None until the
# Phase 4 multi-tenancy ADR. The extension point exists without speculative impl.
tenant_id: ContextVar[UUID | None] = ContextVar("tenant_id", default=None)
