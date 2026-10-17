"""What every agent's thread has in common: its finished requests (the chat), how a run ended, and the site's repo
as the agents may use it. Each agent has its own thread per site (the edit agent's id is the `projectId`, the create
agent's `<projectId>:create`), and the chat shows both."""

from typing import Annotated, NotRequired, Protocol, TypedDict

from app.constants.ai_edit import MAX_TURNS
from app.schemas.site import SiteSnapshot

# Graph steps a run may take (LangGraph's default is 25): a creation writes a file per step.
RECURSION_LIMIT = 100


class SiteFiles(Protocol):
    """The site's repo, as an agent may use it: read a commit, and commit a change on top of one."""

    async def snapshot(self, commit_sha: str) -> SiteSnapshot: ...

    async def commit(self, base_commit: str, changes: dict[str, str | None], message: str) -> str: ...


class QuestionAnswer(TypedDict):
    question: str
    answer: str
    # The user chatted about the question instead of answering it ("Chat about this"): the AI replied and asked again.
    chat: NotRequired[bool]
    # What the AI did before it asked (the create agent's progress), shown above the question.
    steps: NotRequired[list[str]]


class UserReply(TypedDict):
    """What a paused run is resumed with after a question: the user's text, and whether it answers the question or is
    a message about it (`chat`)."""

    answer: str
    chat: bool


def read_reply(value: object) -> UserReply:
    """The resume value of a question, as `UserReply` (a plain string is an answer)."""
    if isinstance(value, dict):
        return UserReply(answer=str(value.get("answer", "")), chat=bool(value.get("chat", False)))
    return UserReply(answer=str(value), chat=False)


class Turn(TypedDict):
    """A finished request in a thread: the chat, and the `<earlier_requests>` of the next prompts."""

    run_id: str
    instruction: str
    target: str | None
    outcome: str
    reply: str
    questions: NotRequired[list[QuestionAnswer]]
    # What the AI did after the last question (the create agent's progress), shown above the reply.
    steps: NotRequired[list[str]]
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
    commit_sha: NotRequired[str]


def keep_recent(old: list[Turn], new: list[Turn]) -> list[Turn]:
    """The `turns` reducer: new turns appended, the oldest dropped past `MAX_TURNS`."""
    return [*old, *new][-MAX_TURNS:]


Turns = Annotated[list[Turn], keep_recent]
