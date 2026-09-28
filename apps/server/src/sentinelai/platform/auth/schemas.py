"""Auth request/response schemas — api-design.md §9, security-architecture.md §5.

The login contract is the documented one: a JSON body of ``{ email, password }`` (api-design.md
§9), **not** an OAuth2 password-grant form post. The success body is
``{ access_token, expires_at }`` per security-architecture.md §5's flow, plus the ``token_type``
that tells a client how to present it, wrapped in the standard §2.4 envelope like every other
``/api/v1`` response.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, SecretStr


class LoginRequest(BaseModel):
    """Password-login credentials."""

    # Plain `str`, not `EmailStr`: `email-validator` is not a dependency of this app, and the
    # lookup is an exact lowercased match against a stored address, so RFC-shape validation
    # would add a dependency without changing which rows can be resolved.
    email: str = Field(min_length=3, max_length=320)
    # SecretStr so the value cannot leak through a model repr, log line, or validation error.
    password: SecretStr = Field(min_length=1)


class LoginResponse(BaseModel):
    """An issued session's bearer token. The plaintext token is returned exactly once."""

    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_at: datetime


class MfaRequiredResponse(BaseModel):
    """The other shape ``POST /auth/login`` can return (api-design.md §9).

    A `200`, not a `401`: the password was correct. What the caller receives instead of a session
    is an ``mfa_token`` to present at ``POST /auth/mfa/verify`` — a credential for a
    half-authenticated principal, short-lived and single-use.
    """

    mfa_required: Literal[True] = True
    mfa_token: str
    expires_at: datetime


class MfaVerifyRequest(BaseModel):
    """The second-factor exchange: the ``mfa_token`` from login plus a code."""

    mfa_token: str = Field(min_length=1, max_length=512)
    # Accepts a TOTP code or a recovery code, so the bound is the longer of the two shapes. The
    # server never tells the client which it matched.
    code: str = Field(min_length=1, max_length=64)


class RefreshRequest(BaseModel):
    """The session to rotate.

    The token travels in the body rather than the ``Authorization`` header because refresh is the
    one endpoint whose credential may already be past the point where a header would be accepted
    by the auth dependency — and because ADR-0010 A3 scopes the refresh credential to this
    endpoint alone. The cookie transport A3 specifies is not yet built; see ADR-0010's status.
    """

    refresh_token: str = Field(min_length=1, max_length=512)
