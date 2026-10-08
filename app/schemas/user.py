from uuid import UUID

from pydantic import BaseModel

from app.constants.auth import Role
from app.schemas.common import CamelModel


class UserCreate(BaseModel):
    auth_user_id: UUID
    github_id: int
    login: str
    name: str | None
    avatar_url: str
    role: Role = Role.USER


class UserUpdate(BaseModel):
    """Partial update: only fields that are set are written."""

    auth_user_id: UUID | None = None
    login: str | None = None
    name: str | None = None
    avatar_url: str | None = None
    role: Role | None = None


class UserRead(CamelModel):
    """The SPA's `SessionUser`."""

    id: UUID
    login: str
    name: str | None
    avatar_url: str
    role: Role
