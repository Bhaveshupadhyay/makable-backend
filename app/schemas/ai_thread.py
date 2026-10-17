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
    # A message about the question ("Chat about this"), not its answer.
    chat: bool = False
    # What the AI did before it asked (a creation's progress).
    steps: list[str] = Field(default_factory=list)


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
    # What the AI did after the last question (a creation's progress: planning, the files it wrote...).
    steps: list[str] = Field(default_factory=list)
    commit_sha: str | None = None
    created_at: datetime


class AiPreview(CamelModel):
    """Edits waiting for the preview's verdict: what they change, and the files as they'd be (None: deleted)."""

    summary: str
    files: dict[str, str | None]


class AiPausedRun(CamelModel):
    """A run waiting for the user (the question it asked: answer it with `/ai/edit/{projectId}/answer`) or for the
    preview (the edits to try: report with `/ai/edit/{projectId}/result`)."""

    run_id: UUID
    # Which agent's run: an edit's, or the site's creation (its questions; answer them with
    # `/ai/create/{projectId}/answer`).
    mode: Literal["edit", "create"] = "edit"
    instruction: str
    target: str | None
    question: Question | None = None
    # What was asked and said earlier in this run, before `question` (an answer that wasn't one, a chat about it).
    questions: list[AiQuestionAnswer] = Field(default_factory=list)
    # What the AI did before it asked `question` (a creation's progress).
    steps: list[str] = Field(default_factory=list)
    preview: AiPreview | None = None


class AiThreadRead(CamelModel):
    """The site's chat, oldest first, and the run that's waiting for the user, if one is."""

    project_id: UUID
    status: Literal["idle", "needs_input", "awaiting_result"]
    turns: list[AiTurnRead]
    run: AiPausedRun | None = None
