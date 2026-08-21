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
