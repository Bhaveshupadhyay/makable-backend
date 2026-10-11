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
    default_branch: str
    description: str | None = None
    archived: bool = False


class GithubFile(BaseModel):
    """A file's text and its blob SHA, which a later write must send back."""

    content: str
    sha: str


class GithubDirEntry(BaseModel):
    name: str
    path: str
    type: str


class GithubBranchHead(BaseModel):
    """The commit a branch points to, and that commit's tree."""

    commit_sha: str
    tree_sha: str


class GithubTreeChange(BaseModel):
    """One file in a new commit: its new text, its bytes (a binary file, e.g. an image), or None to delete it."""

    path: str
    content: str | bytes | None


class GithubActionsKey(BaseModel):
    """The public key a repo's Actions secrets are encrypted with."""

    key_id: str
    key: str


class GithubWorkflowRun(BaseModel):
    status: str
    conclusion: str | None = None
    head_sha: str
    html_url: str
    created_at: str
