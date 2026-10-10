import json
import re
from typing import Annotated, Any, Literal, Self
from uuid import UUID

from pydantic import Field, ValidationError, field_serializer, field_validator, model_validator

from app.constants.ai_edit import MAX_FILE_CHARS, SAFE_REPO_PATH
from app.constants.workspace import (
    MAX_AI_HISTORY_CHARS,
    MAX_CHUNKS,
    MAX_DELETES_PER_SAVE,
    MAX_FILES_PER_TEMPLATE,
    MAX_JSON_PART_CHARS,
    MAX_PARTS_PER_SAVE,
)
from app.schemas.ai_edit import RepoPath
from app.schemas.common import CamelModel
from app.schemas.portfolio import Portfolio, TemplateId
from app.schemas.session import AiHistoryFile, IsoDateTime, MessageChunk

Sha = Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]

_CHUNK = re.compile(r"messages/(\d{4})\.json")
_AI_HISTORY = re.compile(r"ai-history/[a-z0-9][a-z0-9-]{0,63}\.json")
_FILE = re.compile(r"files/([a-z0-9][a-z0-9-]{0,63})/(.+)")


def check_part_path(path: str) -> str:
    """A file a save may write or delete, relative to the project's folder. `state.json` is the server's."""
    if _AI_HISTORY.fullmatch(path):
        return path
    chunk = _CHUNK.fullmatch(path)
    if chunk:
        if not 1 <= int(chunk.group(1)) <= MAX_CHUNKS:
            raise ValueError("chunk number out of range")
        return path
    file = _FILE.fullmatch(path)
    if file and SAFE_REPO_PATH.fullmatch(file.group(2)):
        return path
    raise ValueError("must be messages/NNNN.json, ai-history/<template>.json or files/<template>/<path>")


def chunk_path(index: int) -> str:
    """The file of the `index`-th message chunk (1-based)."""
    return f"messages/{index:04d}.json"


PartPath = Annotated[str, Field(max_length=400)]


class WorkspaceConversation(CamelModel):
    # A step of the SPA's guided chat; the SPA checks it against its own steps.
    step: Annotated[str, Field(max_length=50)]
    portfolio: Portfolio | None
    github_login: Annotated[str, Field(max_length=100)] | None = None


class WorkspaceState(CamelModel):
    """`state.json`: the small part of a session that changes on every save, and an index of the other files."""

    format: Literal["makable-workspace"]
    version: Literal[1]
    saved_at: IsoDateTime
    # Who saved it. Informational.
    login: Annotated[str, Field(max_length=100)] | None
    # A permanent id for the site; its folder in the workspace repo.
    project_id: UUID
    conversation: WorkspaceConversation
    # The chat is in `messages/0001.json` to `messages/<messageChunks>.json`.
    message_chunks: Annotated[int, Field(ge=0, le=MAX_CHUNKS)]
    # AI-edited files per template id, each at `files/<template>/<path>`.
    files: dict[TemplateId, Annotated[list[RepoPath], Field(max_length=MAX_FILES_PER_TEMPLATE)]]
    # Templates with AI history, each at `ai-history/<template>.json`.
    ai_history: Annotated[list[TemplateId], Field(max_length=MAX_FILES_PER_TEMPLATE)]


class WorkspacePart(CamelModel):
    """A file of the session as the SPA wrote it. JSON files are validated, then written exactly as sent."""

    path: PartPath
    content: str

    @field_validator("path")
    @classmethod
    def _known_path(cls, path: str) -> str:
        return check_part_path(path)

    @model_validator(mode="after")
    def _valid_content(self) -> Self:
        try:
            if _AI_HISTORY.fullmatch(self.path):
                _check_length(self.content, MAX_AI_HISTORY_CHARS)
                AiHistoryFile.model_validate(json.loads(self.content))
            elif _CHUNK.fullmatch(self.path):
                _check_length(self.content, MAX_JSON_PART_CHARS)
                MessageChunk.model_validate(json.loads(self.content))
            else:
                _check_length(self.content, MAX_FILE_CHARS)
        except ValidationError as err:
            raise ValueError(f"{self.path}: {err.errors()[0]['msg']}") from None
        except json.JSONDecodeError:
            raise ValueError(f"{self.path}: not valid JSON") from None
        return self


def _check_length(content: str, limit: int) -> None:
    if len(content) > limit:
        raise ValueError(f"must be at most {limit} characters")


class WorkspaceSave(CamelModel):
    """A save: the new state, the files that changed since the last save, and the files that went away.

    A big save (the first one after importing a long session) is sent in batches: the earlier ones carry only
    files, and the last one the state, so the saved state never lists files that aren't there yet.
    """

    # The SHA of the state.json this save replaces, or None for the first save. A different SHA on GitHub
    # means another device saved since, and nothing is written.
    base_sha: Sha | None = None
    # None for an earlier batch of a big save: its files are written, the state stays as it is.
    state: WorkspaceState | None = None
    parts: Annotated[list[WorkspacePart], Field(max_length=MAX_PARTS_PER_SAVE, default_factory=list)]
    deletes: Annotated[list[PartPath], Field(max_length=MAX_DELETES_PER_SAVE, default_factory=list)]

    @field_validator("deletes")
    @classmethod
    def _known_deletes(cls, paths: list[str]) -> list[str]:
        return [check_part_path(p) for p in paths]

    @model_validator(mode="after")
    def _no_overlap(self) -> Self:
        if {p.path for p in self.parts} & set(self.deletes):
            raise ValueError("a file can't be both written and deleted")
        return self


class WorkspaceStateRead(CamelModel):
    """A saved session's state, with the SHA a later save must send back."""

    state: WorkspaceState
    sha: Sha

    @field_serializer("state")
    def _as_saved(self, state: WorkspaceState) -> dict[str, Any]:
        # Exactly the fields the file has: the SPA's schema has optional fields that mustn't come back as null.
        return state.model_dump(mode="json", by_alias=True, exclude_unset=True)


class WorkspacePartRead(CamelModel):
    path: str
    content: str


class WorkspaceSaved(CamelModel):
    # The SHA of the saved state: what the next save sends back. For a batch without a state, the state's SHA
    # as it was (None if there's none yet).
    sha: Sha | None
    repo_url: str
