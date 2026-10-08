from uuid import UUID

from pydantic import BaseModel


class GithubUser(BaseModel):
    id: int
    login: str
    name: str | None = None
    avatar_url: str


class GithubCredentialUpsert(BaseModel):
    """What's stored for a user. The token is already encrypted."""

    user_id: UUID
    access_token: str
