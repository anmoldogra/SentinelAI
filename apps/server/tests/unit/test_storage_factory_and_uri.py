"""Unit tests for object-storage composition and the ``s3://`` URI convention (ADR-0008).

Covers the configuration handling the factory performs (endpoint/bucket/credentials/region come
from ``Settings``, never from literals) and the addressing helpers that map a CEM ``payload_ref``
onto a ``(bucket, key)`` pair.
"""

from __future__ import annotations

import pytest

from sentinelai.platform.config import Settings
from sentinelai.platform.storage import (
    InvalidObjectUri,
    MinioObjectStorage,
    build_object_storage,
    build_object_uri,
    get_object_storage,
    parse_object_uri,
)

# --- URI helpers ------------------------------------------------------------


def test_build_and_parse_round_trip() -> None:
    uri = build_object_uri("evidence-bucket", "evidence/osint/web_page/abc-123")
    assert uri == "s3://evidence-bucket/evidence/osint/web_page/abc-123"
    assert parse_object_uri(uri) == ("evidence-bucket", "evidence/osint/web_page/abc-123")


def test_parse_keeps_slashes_in_the_key() -> None:
    assert parse_object_uri("s3://b/a/b/c.bin") == ("b", "a/b/c.bin")


@pytest.mark.parametrize(
    "uri",
    [
        "https://b/k",  # wrong scheme
        "s3://bucket-only",  # no key
        "s3://",  # empty
        "s3:///key",  # empty bucket
        "s3://bucket/",  # empty key
        "",
    ],
)
def test_malformed_uris_raise_invalid_object_uri(uri: str) -> None:
    with pytest.raises(InvalidObjectUri):
        parse_object_uri(uri)


# --- factory / configuration ------------------------------------------------


def test_factory_builds_the_adapter_from_settings() -> None:
    cfg = Settings(
        storage_endpoint_url="http://minio.internal:9000",
        storage_bucket="evidence-prod",
        storage_region="ap-south-1",
    )
    storage = build_object_storage(cfg)
    assert isinstance(storage, MinioObjectStorage)
    assert storage._endpoint_url == "http://minio.internal:9000"
    assert storage._region == "ap-south-1"


def test_factory_falls_back_to_the_default_settings_object() -> None:
    assert isinstance(build_object_storage(), MinioObjectStorage)


def test_credentials_are_read_from_settings_not_hard_coded() -> None:
    cfg = Settings(storage_access_key="rotated-ak", storage_secret_key="rotated-sk")  # type: ignore[arg-type]
    storage = build_object_storage(cfg)
    assert isinstance(storage, MinioObjectStorage)
    assert storage._access_key == "rotated-ak"
    assert storage._secret_key == "rotated-sk"


def test_secret_settings_do_not_leak_through_repr() -> None:
    """A Settings dump (logs, error reports) must not expose storage credentials."""
    cfg = Settings(storage_secret_key="super-secret-value")  # type: ignore[arg-type]
    assert "super-secret-value" not in repr(cfg)


def test_get_object_storage_returns_the_instance_from_app_state() -> None:
    sentinel = object()
    request = _RequestStub(sentinel)
    assert get_object_storage(request) is sentinel  # type: ignore[arg-type]


def test_get_object_storage_fails_loudly_when_the_composition_root_did_not_run() -> None:
    with pytest.raises(RuntimeError, match="not initialized"):
        get_object_storage(_RequestStub(None))  # type: ignore[arg-type]


class _AppStub:
    def __init__(self, storage: object | None) -> None:
        self.state = type("_State", (), {})()
        if storage is not None:
            self.state.object_storage = storage  # type: ignore[attr-defined]


class _RequestStub:
    def __init__(self, storage: object | None) -> None:
        self.app = _AppStub(storage)
