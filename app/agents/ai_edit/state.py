"""What the AI edit agent saves, and what a run gets that isn't saved.

The state is checkpointed per site (thread id = the site's `projectId`), so it holds only small, plain values: the
thread's finished requests (`turns`, the chat) and the current run's instruction, the model's replies, the repair
prompts and the result. Never file contents, and never our own types (the checkpoint serializer warns about, and
will block, types it doesn't know). The request with the browser's files, and the model, come in through the run's
context (`EditRun`) instead.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated, Any, NotRequired, TypedDict
from uuid import UUID

from app.clients.model import ModelClient
from app.constants.ai_edit import MAX_TARGET_LABEL, MAX_TURNS
from app.schemas.ai_edit import AiEditRequest, AiEditTarget


class Turn(TypedDict):
    """A finished request in the thread: the chat, and the `<earlier_requests>` of the next prompts."""

    run_id: str
    instruction: str
    target: str | None
    outcome: str
    reply: str
    commit_sha: str | None
    created_at: str


class RunResult(TypedDict):
    """How a run ended. `outcome` is an `AiEditOutcome` value."""

    outcome: str
    tier: NotRequired[int]
    summary: NotRequired[str]
    edits: NotRequired[list[dict[str, str]]]
    reason: NotRequired[str]
    problem: NotRequired[str]


def _keep_recent(old: list[Turn], new: list[Turn]) -> list[Turn]:
    return [*old, *new][-MAX_TURNS:]


class EditState(TypedDict, total=False):
    # The thread's finished requests, oldest first. Kept across runs.
    turns: Annotated[list[Turn], _keep_recent]
    # The current run's fields, all set by its input (`run_input`), so a run never sees an earlier, cancelled or
    # failed one's.
    run_id: str
    instruction: str
    target: str | None
    # What followed the first prompt: the model's rejected replies and the repair prompts.
    messages: list[dict[str, str]]
    reply: str
    # Model calls made.
    attempt: int
    problem: str
    result: RunResult | None


@dataclass
class EditRun:
    """A run's context: the request (with the browser's files) and the model. Not saved."""

    request: AiEditRequest
    pick_model: Callable[[], Awaitable[ModelClient]]
    model: ModelClient | None = None
    # The first prompt, built once per run (it has the files in it).
    user_prompt: str = ""

    async def model_client(self) -> ModelClient:
        """The model, picked on the first call: a Tier 2 rule never needs one."""
        if self.model is None:
            self.model = await self.pick_model()
        return self.model


def run_input(run_id: UUID, request: AiEditRequest) -> EditState:
    """A new run's input. Every per-run field is set."""
    return {
        "run_id": str(run_id),
        "instruction": request.instruction,
        "target": target_label(request.target),
        "messages": [],
        "reply": "",
        "attempt": 0,
        "problem": "",
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
