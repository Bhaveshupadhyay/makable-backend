"""What the AI edit agent saves, and what a run gets that isn't saved.

The state is checkpointed per site (thread id = the site's `projectId`), so it holds only small, plain values: the
thread's finished requests (`turns`, the chat) and the current run's instruction, selection, which parts of which
files the model was shown (`context`: references, not the code), the model's replies, the repair reports and the
result. Never file contents, and never our own types (the checkpoint serializer warns about, and will block, types it
doesn't know). The code is read from the site's repo at the run's base commit, through the run's context (`EditRun`),
which isn't saved either.
"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Annotated, Any, NotRequired, Protocol, TypedDict
from uuid import UUID

from app.clients.model import ModelClient
from app.constants.ai_edit import MAX_TARGET_LABEL, MAX_TURNS
from app.schemas.ai_edit import AiEditRequest, AiEditTarget
from app.schemas.site import SiteSnapshot


class SiteFiles(Protocol):
    """The site's repo, as the agent may use it: read a commit, and commit a change on top of one."""

    async def snapshot(self, commit_sha: str) -> SiteSnapshot: ...

    async def commit(self, base_commit: str, changes: dict[str, str | None], message: str) -> str: ...


class QuestionAnswer(TypedDict):
    question: str
    answer: str


class Turn(TypedDict):
    """A finished request in the thread: the chat, and the `<earlier_requests>` of the next prompts."""

    run_id: str
    instruction: str
    target: str | None
    outcome: str
    reply: str
    questions: NotRequired[list[QuestionAnswer]]
    commit_sha: str | None
    created_at: str


class Selection(TypedDict):
    """The selected element's place in the code (found again if the preview's line was off) and how it looks."""

    path: str
    line: int
    tag: str
    text: str


class ContextRef(TypedDict):
    """Lines `start`-`end` (1-based, inclusive) of a file the model was shown. `outline` shows the file's outline
    instead (its imports and declarations)."""

    path: str
    start: int
    end: int
    outline: NotRequired[bool]


class RunResult(TypedDict):
    """How a run ended. `outcome` is an `AiEditOutcome` value."""

    outcome: str
    tier: NotRequired[int]
    summary: NotRequired[str]
    edits: NotRequired[list[dict[str, str]]]
    reason: NotRequired[str]
    problem: NotRequired[str]
    commit_sha: NotRequired[str]


def _keep_recent(old: list[Turn], new: list[Turn]) -> list[Turn]:
    return [*old, *new][-MAX_TURNS:]


class EditState(TypedDict, total=False):
    # The thread's finished requests, oldest first. Kept across runs.
    turns: Annotated[list[Turn], _keep_recent]
    # The current run's fields, all set by its input (`run_input`), so a run never sees an earlier, cancelled or
    # failed one's.
    run_id: str
    # The request as sent, without its picture: a paused run continues from it (with the answer's base commit).
    request: dict[str, Any]
    instruction: str
    target: str | None
    base_commit: str
    selection: Selection | None
    # What the model was shown of the code, in order.
    context: list[ContextRef]
    context_rounds: int
    # What followed the first prompt: the model's replies, and the messages sent back (repair reports, or a
    # reference to code it asked for, which is read again when the prompt is built).
    messages: list[dict[str, Any]]
    reply: str
    # Model calls made, and the model's name (the run can end in a later request than its model calls).
    attempt: int
    model: str
    # What the model asked the user, and their answers.
    questions: list[QuestionAnswer]
    # Times the preview failed on the model's edits.
    preview_rounds: int
    problem: str
    # Where the run goes next after `check` or `preview`: "read_more", "ask_user", "generate" (a repair), "preview",
    # "commit" or "finish".
    step: str
    result: RunResult | None


@dataclass
class EditRun:
    """A run's context: the request, the site's repo and the model. Not saved."""

    request: AiEditRequest
    pick_model: Callable[[], Awaitable[ModelClient]]
    site: SiteFiles
    model: ModelClient | None = None
    # The last prompt the model got (for `AI_DEBUG`).
    model_input: str = ""
    # The files the change made, as they are now (None: deleted), for the response.
    changed_files: dict[str, str | None] = field(default_factory=dict)
    # The commit, once it started, and the result it's for: it's finished even if the run is cancelled (see the
    # `commit` node), and the service records it then.
    commit_task: asyncio.Future[str] | None = None
    committing: RunResult | None = None
    _files: dict[str, str] | None = None

    async def model_client(self) -> ModelClient:
        """The model, picked on the first call: a Tier 2 rule never needs one."""
        if self.model is None:
            self.model = await self.pick_model()
        return self.model

    async def files(self) -> dict[str, str]:
        """The site's text files at the request's base commit (read once per run)."""
        if self._files is None:
            self._files = (await self.site.snapshot(self.request.base_commit)).files
        return self._files


def run_input(run_id: UUID, request: AiEditRequest) -> EditState:
    """A new run's input. Every per-run field is set."""
    return {
        "run_id": str(run_id),
        "request": request.model_dump(mode="json", exclude={"screenshot"}),
        "instruction": request.instruction,
        "target": target_label(request.target),
        "base_commit": request.base_commit,
        "selection": None,
        "context": [],
        "context_rounds": 0,
        "messages": [],
        "reply": "",
        "attempt": 0,
        "model": "",
        "questions": [],
        "preview_rounds": 0,
        "problem": "",
        "step": "",
        "result": None,
    }


def target_label(target: AiEditTarget | None) -> str | None:
    """A short label for the selection in the chat and the prompt, e.g. `Ada > <a> "Contact"`."""
    if target is None:
        return None
    section = target.section
    where = (section.heading or section.id or section.tag) if section else None
    text = f' "{target.text[:60]}"' if target.text else ""
    label = f"<{target.tag}>{text}"
    return (f"{where} > {label}" if where else label)[:MAX_TARGET_LABEL]


def result_of(state: EditState | dict[str, Any]) -> RunResult:
    """The finished run's result."""
    result: RunResult | None = state.get("result")
    if result is None:
        raise RuntimeError("the AI edit run ended without a result")
    return result
