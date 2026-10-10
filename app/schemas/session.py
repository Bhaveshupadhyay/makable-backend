"""A builder session as one file: the SPA's `@makable/shared` `session.ts`, with the same caps. Session files
come from the browser, so they're untrusted and validated before they're written to the user's repo."""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AfterValidator, Field

from app.constants.ai_edit import MAX_FILE_CHARS
from app.constants.workspace import MAX_MESSAGE_CHARS, MAX_MESSAGES, MAX_STORED_TURNS
from app.schemas.ai_edit import AiEditTurn, RepoPath
from app.schemas.common import CamelModel
from app.schemas.portfolio import Portfolio, TemplateId


def _iso_datetime(value: str) -> str:
    # Kept as the string the SPA wrote, so the file round-trips unchanged; only checked here.
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError("must be an ISO 8601 date and time") from None
    if parsed.tzinfo is None:
        raise ValueError("must include a time zone")
    return value


Id = Annotated[str, Field(min_length=1, max_length=100)]


class SessionMessage(CamelModel):
    id: Id
    role: Literal["user", "assistant"]
    text: Annotated[str, Field(max_length=MAX_MESSAGE_CHARS)]
    widget: Literal["template-picker", "connect-github"] | None = None
    replies: Annotated[list[Annotated[str, Field(max_length=200)]], Field(max_length=20)] | None = None


class SessionTurn(AiEditTurn):
    id: Id


class SessionConversation(CamelModel):
    # A step of the SPA's guided chat; the SPA checks it against its own steps.
    step: Annotated[str, Field(max_length=50)]
    messages: Annotated[list[SessionMessage], Field(min_length=1, max_length=MAX_MESSAGES)]
    portfolio: Portfolio | None
    github_login: Annotated[str, Field(max_length=100)] | None = None


class SessionSnapshot(CamelModel):
    format: Literal["makable-session"]
    version: Literal[1]
    exported_at: Annotated[str, AfterValidator(_iso_datetime)]
    # Who saved it (None for a guest). Informational.
    login: Annotated[str, Field(max_length=100)] | None
    # A permanent id for the site; the session's folder in the workspace repo.
    project_id: UUID
    conversation: SessionConversation
    # AI-edited files per template id: repo path -> full contents.
    file_edits: dict[TemplateId, dict[RepoPath, Annotated[str, Field(max_length=MAX_FILE_CHARS)]]]
    # Finished AI requests per template id, oldest first.
    ai_history: dict[TemplateId, Annotated[list[SessionTurn], Field(max_length=MAX_STORED_TURNS)]]
