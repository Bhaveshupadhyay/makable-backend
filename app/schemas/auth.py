from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.schemas.common import CamelModel
from app.schemas.user import UserRead


class OAuthState(BaseModel):
    """What the login step hands to the callback, in a signed cookie."""

    code_verifier: str
    return_to: str


class LoginRedirect(BaseModel):
    model_config = ConfigDict(frozen=True)

    authorize_url: str
    state_cookie: str


class SessionTokens(BaseModel):
    """A Supabase session, as it goes into the auth cookies."""

    model_config = ConfigDict(frozen=True)

    access_token: str
    refresh_token: str
    # Seconds until the access token expires.
    expires_in: int


class LoginResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    tokens: SessionTokens
    return_to: str


class AccessTokenClaims(BaseModel):
    """The claims we use from a verified Supabase access token."""

    # Supabase's user id (`auth.users.id`).
    sub: UUID
    exp: datetime


class SessionRead(CamelModel):
    user: UserRead
