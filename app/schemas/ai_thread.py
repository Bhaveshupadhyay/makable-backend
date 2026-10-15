"""A site's AI thread: the chat the SPA shows."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field

from app.constants.ai_edit import AiEditOutcome
from app.schemas.ai_edit import Question
from app.schemas.common import CamelModel


class AiQuestionAnswer(CamelModel):
    question: str
    answer: str


class AiTurnRead(CamelModel):
    """A finished request in the chat: what was asked and what came of it."""

    instruction: str
    # A short label for what was selected, e.g. `Hero > <a> "Contact"`.
    target: str | None
    outcome: AiEditOutcome
    # The change made, or why nothing changed.
    reply: str
    # What the AI asked on the way, and what the user said.
    questions: list[AiQuestionAnswer] = Field(default_factory=list)
    commit_sha: str | None = None
    created_at: datetime


class AiPausedRun(CamelModel):
    """A run waiting for the user: the question it asked (answer it with `/ai/edit/{projectId}/answer`)."""

    run_id: UUID
    instruction: str
    target: str | None
    question: Question


class AiThreadRead(CamelModel):
    """The site's chat, oldest first, and the run that's waiting for the user, if one is."""

    project_id: UUID
    status: Literal["idle", "needs_input"]
    turns: list[AiTurnRead]
    run: AiPausedRun | None = None
