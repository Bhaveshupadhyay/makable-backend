"""What the create agent saves, and what a run gets that isn't saved.

The state is checkpointed in the site's create thread (`<projectId>:create`) and holds only small, plain values: the
user's prompt, the site's name, the plan (paths and what each file is for), which files are written, the problems the
checks found, and the result. Never file contents: the files the model writes live in the run's context (`CreateRun`)
until they're committed, and a creation only pauses before it writes anything (for the questions about the site,
then its name).
"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Annotated, Any, Protocol, TypedDict
from uuid import UUID

from app.agents.thread import QuestionAnswer, RunResult, SiteFiles, Turns
from app.clients.model import ModelClient
from app.constants.ai_create import MAX_STEPS
from app.schemas.ai_create import AiCreateRequest


class NameTakenError(Exception):
    """The user already has a repo with the name a site's name makes (`repo_name`)."""

    def __init__(self, repo_name: str) -> None:
        super().__init__(f"the repository {repo_name} is taken")
        self.repo_name = repo_name


class CreateFailedError(Exception):
    """The model couldn't plan the site, or write one of its files, in its tries. The message says which, and why its
    last reply was rejected."""


class SiteCreator(SiteFiles, Protocol):
    """The site's repo as the create agent uses it: made from the starter files (returns the commit they're at), then
    read and committed to.

    `create` raises `NameTakenError` when the name's repo is taken."""

    async def create(self, name: str) -> str: ...


class PlannedFile(TypedDict):
    path: str
    purpose: str


def add_steps(old: list[str], new: list[str]) -> list[str]:
    """The `steps` reducer: a node's steps are added after the ones before; an empty list clears them (a question was
    answered, or a new run started)."""
    return [*old, *new][-MAX_STEPS:] if new else []


class CreateState(TypedDict, total=False):
    # The thread's finished requests, oldest first. Kept across runs.
    turns: Turns
    # The current run's fields, all set by its input (`create_input`).
    run_id: str
    request: dict[str, Any]
    prompt: str
    # The questions about the site asked first, with the user's answers (and messages about them), and the one waiting
    # for an answer (a `Question`, as the SPA gets it).
    brief: list[QuestionAnswer]
    brief_question: dict[str, Any] | None
    # The site's name; set when the site already has its repo, else asked after the questions about the site, with
    # the names the model suggested then (or one from the prompt).
    name: str
    suggested_names: list[str]
    # The next name question and its options, when it isn't the first one (empty: the first, with the suggested names):
    # the model's answer to what the user said instead of a name, or that the name's repo is taken.
    name_question: str
    name_options: list[str]
    # What the user answered to the name question last (read by the `read_name` node), and whether it was a message
    # about the question ("Chat about this") rather than an answer.
    name_reply: str
    name_chat: bool
    name_tries: int
    # The commit the site's repo is at when the files are written (the starter's, for a new site).
    base_commit: str
    summary: str
    plan: list[PlannedFile]
    # The next file of the plan to write, and the paths written.
    index: int
    written: list[str]
    # What the checks found, per path, and the rounds of fixes so far.
    problems: dict[str, list[str]]
    fix_rounds: int
    # Model calls made, and the model's name.
    attempt: int
    model: str
    questions: list[QuestionAnswer]
    # What the run did since the last question was answered (the progress the browser was shown): the next question,
    # or the finished turn, keeps them for the chat.
    steps: Annotated[list[str], add_steps]
    # Where the run goes next: "brief", "ask_brief", "ask_name", "read_name", "setup", "plan", "write", "verify", "fix",
    # "commit" or "finish".
    step: str
    result: RunResult | None


@dataclass
class CreateRun:
    """A creation's context: the request, the site's repo and the model, and the files written so far. Not saved."""

    request: AiCreateRequest
    pick_model: Callable[[], Awaitable[ModelClient]]
    site: SiteCreator
    model: ModelClient | None = None
    model_input: str = ""
    # The files the model wrote, by path (committed together at the end).
    files: dict[str, str] = field(default_factory=dict)
    # The commit, once it started, and the run's state then, with the summary the chat gets: it's finished even if
    # the run is cancelled (see the `commit` node), and the service records it then.
    commit_task: asyncio.Future[str] | None = None
    committing: CreateState | None = None
    _base: dict[str, str] | None = None

    async def model_client(self) -> ModelClient:
        if self.model is None:
            self.model = await self.pick_model()
        return self.model

    async def base_files(self, commit_sha: str) -> dict[str, str]:
        """The site's text files at the base commit (the starter files, for a new site), read once per run."""
        if self._base is None:
            self._base = (await self.site.snapshot(commit_sha)).files
        return self._base

    async def site_files(self, commit_sha: str) -> dict[str, str]:
        """The site as it will be: the base commit's files with the ones written so far over them."""
        return {**await self.base_files(commit_sha), **self.files}


def create_input(run_id: UUID, request: AiCreateRequest, *, name: str | None, base_commit: str | None) -> CreateState:
    """A new creation's input. `name` and `base_commit` are the site's when it already has its repo."""
    return {
        "run_id": str(run_id),
        "request": request.model_dump(mode="json"),
        "prompt": request.prompt,
        "brief": [],
        "brief_question": None,
        "name": name or "",
        "suggested_names": [],
        "name_question": "",
        "name_options": [],
        "name_reply": "",
        "name_chat": False,
        "name_tries": 0,
        "base_commit": base_commit or "",
        "summary": "",
        "plan": [],
        "index": 0,
        "written": [],
        "problems": {},
        "fix_rounds": 0,
        "attempt": 0,
        "model": "",
        "questions": [],
        "steps": [],
        "step": "",
        "result": None,
    }
