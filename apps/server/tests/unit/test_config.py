"""Configuration profile + startup-validation tests (Wave-1 W1-01).

The frozen sources are implementation-wave-1.md §5 (the five profiles and their fail-closed
rules), ADR-0010 (opaque sessions — the JWT fields are reserved, not active), and
security-architecture.md (no placeholder secret / no `dev` KMS in a production-grade profile).

Settings are constructed with ``_env_file=None`` so a developer's local ``.env`` never leaks
into the assertions — every value under test is passed explicitly.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from sentinelai.platform.config import ConfigurationError, Settings

# A minimally-valid production-grade configuration used as the baseline the negative tests mutate.
_PROD_OK: dict[str, object] = {
    "app_env": "production",
    "kms_provider": "vault_transit",
    "database_url": "postgresql+asyncpg://u:p@db:5432/sentinelai",
    "storage_access_key": "a-real-injected-access-key",
    "storage_secret_key": "a-real-injected-secret-key",
    "vault_token": "a-real-injected-vault-token",
}


def _settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, **overrides)  # type: ignore[arg-type]


# --- profile classification ------------------------------------------------
def test_development_is_not_production_grade() -> None:
    s = _settings(app_env="development")
    assert s.is_production is False
    assert s.is_air_gapped is False
    assert s.is_classified is False


def test_testing_is_not_production_grade() -> None:
    assert _settings(app_env="testing").is_production is False


@pytest.mark.parametrize("profile", ["production", "air-gapped", "classified"])
def test_air_gapped_and_classified_are_production_grade(profile: str) -> None:
    # implementation-wave-1.md §5: air-gapped/classified are hardening overlays ON production —
    # every fail-closed production rule must apply to them too.
    assert _settings(**{**_PROD_OK, "app_env": profile}).is_production is True


def test_classified_implies_air_gapped() -> None:
    s = _settings(**{**_PROD_OK, "app_env": "classified", "kms_provider": "pkcs11"})
    assert s.is_classified is True
    assert s.is_air_gapped is True


def test_prod_alias_normalizes_to_production() -> None:
    assert _settings(**{**_PROD_OK, "app_env": "PROD"}).app_env == "production"


def test_unknown_profile_is_rejected_at_construction() -> None:
    with pytest.raises(ValidationError):  # pydantic wraps the validator's ValueError
        _settings(app_env="staging")


# --- validate_for_profile: development is permissive -----------------------
def test_validate_for_profile_development_passes_with_defaults() -> None:
    _settings(app_env="development").validate_for_profile()  # must not raise


# --- validate_for_profile: production-grade fail-closed rules ---------------
def test_production_rejects_dev_kms_provider() -> None:
    with pytest.raises(ConfigurationError, match="KMS_PROVIDER"):
        _settings(**{**_PROD_OK, "kms_provider": "dev"}).validate_for_profile()


def test_production_rejects_placeholder_storage_secret() -> None:
    with pytest.raises(ConfigurationError, match="STORAGE_SECRET_KEY"):
        _settings(**{**_PROD_OK, "storage_secret_key": "minioadmin"}).validate_for_profile()


def test_production_rejects_sync_database_driver() -> None:
    with pytest.raises(ConfigurationError, match="DATABASE_URL"):
        _settings(
            **{**_PROD_OK, "database_url": "postgresql://u:p@db:5432/sentinelai"}
        ).validate_for_profile()


def test_production_error_never_leaks_the_secret_value() -> None:
    with pytest.raises(ConfigurationError) as exc:
        _settings(**{**_PROD_OK, "storage_secret_key": "minioadmin"}).validate_for_profile()
    assert "minioadmin" not in str(exc.value)  # message names the field, never the value


def test_fully_valid_production_config_passes() -> None:
    _settings(**_PROD_OK).validate_for_profile()  # must not raise


def test_classified_rejects_non_hardware_kms() -> None:
    # A classified deployment must be backed by an HSM / managed KMS, never software.
    with pytest.raises(ConfigurationError, match="KMS_PROVIDER"):
        _settings(
            **{**_PROD_OK, "app_env": "classified", "kms_provider": "dev"}
        ).validate_for_profile()


def test_classified_accepts_pkcs11() -> None:
    _settings(
        **{**_PROD_OK, "app_env": "classified", "kms_provider": "pkcs11"}
    ).validate_for_profile()
