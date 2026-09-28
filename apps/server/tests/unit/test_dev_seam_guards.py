"""The developer seams must be unreachable on a production-grade profile — Wave 3.1.

Two seams exist so local work is not blocked on an unbuilt login screen, and both are documented
as safe. Documented is not enforced, and neither had a test: a refactor that dropped either guard
would have produced a build that mints sessions without a password on a classified deployment, and
nothing would have failed.

`is_production` is the property both turn on, and it deliberately spans **three** profiles —
``air-gapped`` and ``classified`` are hardening overlays on ``production``, so a guard written as
``app_env == "production"`` would leave exactly the two most sensitive deployments open. That is
the mistake these tests are shaped to catch.
"""

from __future__ import annotations

import pytest

from sentinelai.cli.admin import issue_dev_token
from sentinelai.platform.config import VALID_PROFILES, Settings, settings

_PRODUCTION_GRADE = ("production", "air-gapped", "classified")
_DEVELOPMENT_GRADE = ("development", "testing")


@pytest.mark.parametrize("profile", _PRODUCTION_GRADE)
def test_every_production_grade_profile_reports_is_production(profile: str) -> None:
    assert Settings(app_env=profile).is_production is True


@pytest.mark.parametrize("profile", _DEVELOPMENT_GRADE)
def test_development_profiles_do_not(profile: str) -> None:
    assert Settings(app_env=profile).is_production is False


def test_the_two_sets_cover_every_valid_profile() -> None:
    """If a sixth profile is ever added, this fails rather than letting it default to unguarded."""
    assert set(_PRODUCTION_GRADE) | set(_DEVELOPMENT_GRADE) == set(VALID_PROFILES)


@pytest.mark.parametrize("profile", _PRODUCTION_GRADE)
async def test_dev_token_minting_is_refused_on_production_grade_profiles(
    profile: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``make dev-token`` mints a real session without a password or a second factor.

    It is the one code path that can produce a valid bearer token while bypassing every check in
    ``AuthService.login``, so its guard is load-bearing. The refusal must happen *before* any
    database or KMS work — a token that is created and then refused has still been created.
    """
    monkeypatch.setattr(settings, "app_env", profile)

    with pytest.raises(SystemExit) as excinfo:
        await issue_dev_token(email="analyst@agency.gov", ttl_days=7, quiet=True)

    message = str(excinfo.value)
    assert profile in message
    assert "refusing" in message.lower()


def test_enroll_mfa_is_reachable_as_a_command() -> None:
    """MFA enforcement without an enrolment path is dead code: nothing would ever set
    ``mfa_enrolled_at``, the login branch would never fire, and §8 would stay unmet in practice.

    ``api-design.md`` §9 documents no enrolment endpoint, so the provisioning CLI is the path —
    the same reason ``create-user`` exists. This asserts the wiring, since a command that is
    defined but not routed from ``_run`` fails only when a human tries it.
    """
    from sentinelai.cli.admin import build_parser

    args = build_parser().parse_args(["enroll-mfa", "--email", "a@b.gov"])
    assert args.command == "enroll-mfa"
    assert args.email == "a@b.gov"
    assert args.replace is False, "re-enrolment must be opt-in — it invalidates the current secret"


def test_enroll_mfa_has_no_production_restriction() -> None:
    """Unlike ``dev-token``, enrolling a real operator's second factor is a legitimate action on
    any profile — refusing it in production would make §8's mandatory factor unprovisionable
    exactly where it matters most."""
    import inspect

    from sentinelai.cli.admin import enroll_mfa, issue_dev_token

    assert "is_production" in inspect.getsource(issue_dev_token)
    assert "is_production" not in inspect.getsource(enroll_mfa)
