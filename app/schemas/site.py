"""A site: its row (whose it is, its name and repo), its files at a commit, and the writes the browser makes."""

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from pydantic import AfterValidator, BaseModel, Field, StringConstraints, model_validator

from app.constants.ai_edit import SAFE_REPO_PATH, AiEditOutcome
from app.constants.site import (
    MAX_COMMIT_MESSAGE,
    MAX_FILE_CHARS,
    MAX_SAVE_CHARS,
    MAX_SAVE_FILES,
    RESERVED_PATH,
    SITE_NAME_MAX,
)
from app.schemas.common import CamelModel

CommitSha = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{40}$")]


def _writable_path(value: str) -> str:
    if not SAFE_REPO_PATH.fullmatch(value):
        raise ValueError("must be a relative path inside the project")
    if RESERVED_PATH.match(value):
        raise ValueError("is one of makable's or git's own files")
    return value


WritablePath = Annotated[str, AfterValidator(_writable_path)]
SiteName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=SITE_NAME_MAX)]


class SiteCreate(BaseModel):
    id: UUID
    user_id: UUID
    name: str
    repo_name: str | None = None


class AiEditRecordCreate(BaseModel):
    """A finished request, for `ai_edit_requests`."""

    site_id: UUID
    user_id: UUID
    instruction: str
    selection: dict[str, Any] | None
    model: str | None
    tier: int | None
    outcome: AiEditOutcome
    summary: str | None
    attempts: int
    commit_sha: str | None = None


class SiteRead(CamelModel):
    project_id: UUID
    name: str
    # The repo's name on the user's GitHub, and its page (None until it's created).
    repo_name: str | None
    repo_url: str | None
    updated_at: datetime


class SiteNew(CamelModel):
    """Creates the site's repo from makable's starter files."""

    name: SiteName


class SiteSnapshot(CamelModel):
    """A site's files at a commit: text files with their contents, and the paths of the others (images...), which
    the preview can't show and the editor can't change."""

    commit_sha: str
    files: dict[str, str]
    binary: list[str]


class SiteFiles(SiteSnapshot):
    site: SiteRead


class SiteSave(CamelModel):
    """The user's manual edits, as one commit on top of `base_commit` (the commit the browser's copy is at)."""

    base_commit: CommitSha
    files: Annotated[
        dict[WritablePath, Annotated[str, Field(max_length=MAX_FILE_CHARS)]], Field(max_length=MAX_SAVE_FILES)
    ]
    deletes: Annotated[list[WritablePath], Field(max_length=MAX_SAVE_FILES)] = Field(default_factory=list)
    message: Annotated[str, StringConstraints(strip_whitespace=True, max_length=MAX_COMMIT_MESSAGE)] | None = None

    @model_validator(mode="after")
    def _within_total_size(self) -> SiteSave:
        if sum(len(content) for content in self.files.values()) > MAX_SAVE_CHARS:
            raise ValueError(f"The files can be at most {MAX_SAVE_CHARS} characters together")
        return self


class SiteSaved(CamelModel):
    commit_sha: str
    # False when nothing differed from `base_commit`: no commit was made.
    changed: bool


class SiteUndo(CamelModel):
    """Undoes the changes of `commit_sha` (e.g. an AI edit) with a new commit on top of `base_commit`."""

    commit_sha: CommitSha
    base_commit: CommitSha


class SiteUndone(CamelModel):
    commit_sha: str
    # The files the undo changed, with their contents now (None: deleted).
    files: dict[str, str | None]
