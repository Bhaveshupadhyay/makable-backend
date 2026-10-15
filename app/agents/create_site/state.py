"""What the create agent saves, and what a run gets that isn't saved.

The state is checkpointed in the site's create thread (`<projectId>:create`) and holds only small, plain values: the
user's prompt, the site's name, the plan (paths and what each file is for), which files are written, the problems the
checks found, and the result. Never file contents: the files the model writes live in the run's context (`CreateRun`)
until they're committed, and a creation only pauses before it writes anything (for the site's name).
"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, TypedDict
from uuid import UUID

from app.agents.thread import QuestionAnswer, RunResult, SiteFiles, Turns
from app.clients.model import ModelClient
from app.schemas.ai_create import AiCreateRequest


class NameTakenError(Exception):
    """The user already has a repo with the name a site's name makes (`repo_name`)."""

    def __init__(self, repo_name: str) -> None:
        super().__init__(f"the repository {repo_name} is taken")
        self.repo_name = repo_name


class CreateFailedError(Exception):
    """The model couldn't plan the site, or write one of its files, in its tries."""


class SiteCreator(SiteFiles, Protocol):
    """The site's repo as the create agent uses it: made from the starter files (returns the commit they're at), then
    read and committed to.

    `create` raises `NameTakenError` when the name's repo is taken."""

    async def create(self, name: str) -> str: ...


class PlannedFile(TypedDict):
    path: str
    purpose: str


class CreateState(TypedDict, total=False):
    # The thread's finished requests, oldest first. Kept across runs.
    turns: Turns
    # The current run's fields, all set by its input (`create_input`).
    run_id: str
    request: dict[str, Any]
    prompt: str
    # The site's name; set when the site already has its repo, else asked.
    name: str
    # Why the last name didn't work (the repo name is taken), for the next question.
    name_problem: str
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
    # Where the run goes next: "ask_name", "setup", "plan", "write", "verify", "fix", "commit" or "finish".
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
    commit_task: asyncio.Future[str] | None = None
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
        "name": name or "",
        "name_problem": "",
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
        "step": "",
        "result": None,
    }
