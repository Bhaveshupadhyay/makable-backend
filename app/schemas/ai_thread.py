"""A site's AI thread: who owns it, its requests as rows, and the chat the SPA shows."""

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel

from app.constants.ai_edit import AiEditOutcome
from app.schemas.common import CamelModel


class AiThreadCreate(BaseModel):
    id: UUID
    user_id: UUID


class AiEditRecordCreate(BaseModel):
    """A finished request, for `ai_edit_requests`."""

    thread_id: UUID
    user_id: UUID
    instruction: str
    selection: dict[str, Any] | None
    model: str | None
    tier: int | None
    outcome: AiEditOutcome
    summary: str | None
    attempts: int
    commit_sha: str | None = None


class AiTurnRead(CamelModel):
    """A finished request in the chat: what was asked and what came of it."""

    instruction: str
    # A short label for what was selected, e.g. `Hero > <a> "Contact"`.
    target: str | None
    outcome: AiEditOutcome
    # The change made, or why nothing changed.
    reply: str
    created_at: datetime


class AiThreadRead(CamelModel):
    """The site's chat, oldest first. `status` grows when runs can pause (questions, preview checks)."""

    project_id: UUID
    status: Literal["idle"]
    turns: list[AiTurnRead]
