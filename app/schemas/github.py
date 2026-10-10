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


class GithubRepo(BaseModel):
    id: int
    name: str
    full_name: str
    private: bool
    html_url: str


class GithubFile(BaseModel):
    """A file's text and its blob SHA, which a later write must send back."""

    content: str
    sha: str


class GithubDirEntry(BaseModel):
    name: str
    path: str
    type: str
