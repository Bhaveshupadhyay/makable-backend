"""Creating a site from a prompt, on the fake GitHub: the name question, the repo from the starter files, the plan, the
files, the checks and their fixes, the commit, and how a creation streams, stops and fails."""

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants.ai_edit import AiEditOutcome
from app.models.site import AiEditRecord
from app.schemas.ai_create import AiCreateAnswer, AiCreateRequest
from app.schemas.chat import ChatMessage
from app.services.ai_edit_service import AiRunInProgressError, AiRunSupersededError
from app.services.site_service import SiteNotFoundError
from app.tests.ai_edit_fixtures import PROJECT_ID, REPO, USER
from app.tests.conftest import AiServices
from app.tests.fakes import FakeGithubClient, FakeModelClient, FakeModelSource
from app.tests.services.test_ai_edit_threads import OTHER_USER

NEW = UUID("77777777-7777-4777-8777-777777777777")
NEW_REPO = "octocat/ada-s-bakery"
PROMPT = 'A site for my bakery called "Ada\'s Bakery", with our menu and opening hours'

PLAN = json.dumps(
    {
        "summary": "A one-page site for Ada's Bakery with its menu and opening hours.",
        "files": [
            {"path": "src/App.tsx", "purpose": "the page"},
            {"path": "src/components/Hero.tsx", "purpose": "the hero"},
            {"path": "src/index.css", "purpose": "colours and fonts"},
        ],
    }
)
CSS = '@import "tailwindcss";\n\n:root {\n  --accent: #d97706;\n}\n'
HERO = (
    "import { Cake } from 'lucide-react'\n\n"
    "export function Hero() {\n"
    '  return <section id="hero"><Cake /><h1>Ada\'s Bakery</h1></section>\n'
    "}\n"
)
APP = (
    "import { Hero } from './components/Hero'\n\n"
    "export function App() {\n"
    "  return (\n"
    "    <main>\n"
    "      <title>Ada's Bakery</title>\n"
    "      <Hero />\n"
    "    </main>\n"
    "  )\n"
    "}\n"
)
BROKEN_APP = APP.replace("import { Hero } from './components/Hero'", "import { Hero } from './components/Heroes'")


def fenced(code: str, lang: str = "tsx") -> str:
    return f"Here is the file:\n```{lang}\n{code}```\n"


def replies(app: str = APP) -> list[str]:
    return [PLAN, fenced(CSS, "css"), fenced(HERO), fenced(app)]


async def collect(events: AsyncIterator[dict[str, Any]]) -> list[dict[str, Any]]:
    return [event async for event in events]


def result_of(events: list[dict[str, Any]]) -> dict[str, Any]:
    [result] = [e for e in events if e["type"] == "result"]
    data: dict[str, Any] = result["data"]
    return data


async def records(session: AsyncSession) -> list[AiEditRecord]:
    session.expire_all()
    return list(await session.scalars(select(AiEditRecord).order_by(AiEditRecord.created_at)))


async def asked_for_a_name(ai_services: AiServices, model: FakeModelClient) -> dict[str, Any]:
    events = await collect(
        await ai_services.create(FakeModelSource(model)).create(USER, AiCreateRequest(project_id=NEW, prompt=PROMPT))
    )
    return result_of(events)


async def test_a_site_is_created_from_a_prompt(
    ai_services: AiServices, github: FakeGithubClient, session: AsyncSession
) -> None:
    model = FakeModelClient(*replies())

    asked = await asked_for_a_name(ai_services, model)

    assert asked["status"] == "needs_input"
    assert asked["question"]["kind"] == "project_name"
    assert asked["question"]["options"] == ["Ada's Bakery"]
    assert github.repos == {}
    thread = await ai_services.edit(FakeModelSource(model)).thread(USER, NEW)
    assert (thread.status, thread.run and thread.run.mode) == ("needs_input", "create")

    answer = AiCreateAnswer(run_id=asked["runId"], answer="Ada's Bakery")
    events = await collect(await ai_services.create(FakeModelSource(model)).answer(USER, NEW, answer))

    progress = [(e["stage"], e["message"]) for e in events if e["type"] == "progress"]
    assert progress == [
        ("setup", "Creating the repository for Ada's Bakery on your GitHub"),
        ("plan", "Planning the site"),
        ("write", "Writing src/index.css"),
        ("write", "Writing src/components/Hero.tsx"),
        ("write", "Writing src/App.tsx"),
        ("check", "Checking the code"),
        ("commit", "Saving the site to your GitHub"),
    ]
    created = result_of(events)
    assert created["status"] == "created"
    assert created["summary"] == "A one-page site for Ada's Bakery with its menu and opening hours."
    assert created["commitSha"] == github.heads[NEW_REPO].commit_sha
    assert (created["site"]["name"], created["site"]["repoName"]) == ("Ada's Bakery", "ada-s-bakery")
    files = github.files[NEW_REPO]
    assert (files["src/App.tsx"].content, files["src/components/Hero.tsx"].content) == (APP, HERO)
    assert files["src/index.css"].content == CSS
    assert "vite.config.ts" in files and "src/main.tsx" in files
    assert [c[1] for c in github.commits] == [
        "Start Ada's Bakery with makable",
        "Create Ada's Bakery from your prompt",
    ]
    thread = await ai_services.edit(FakeModelSource(model)).thread(USER, NEW)
    assert thread.status == "idle"
    [turn] = thread.turns
    assert (turn.instruction, turn.outcome, turn.commit_sha) == (PROMPT, AiEditOutcome.APPLIED, created["commitSha"])
    [named] = turn.questions
    assert (named.question, named.answer) == (
        "What should we call this site? It's also the name of its repository on your GitHub.",
        "Ada's Bakery",
    )
    [record] = await records(session)
    assert (record.outcome, record.attempts, record.model) == (AiEditOutcome.APPLIED, 4, "fake-model")
    # The plan, then each file with the files written before it.
    assert '<file path="src/index.css">\n' + CSS in model.calls[2][0].text
    assert "Write src/App.tsx: the page" in model.calls[3][0].text


async def test_a_taken_name_is_asked_again(ai_services: AiServices, github: FakeGithubClient) -> None:
    github.add_repo("ada-s-bakery", private=False, files={"index.html": "mine"})
    model = FakeModelClient(*replies())
    asked = await asked_for_a_name(ai_services, model)

    first = await collect(
        await ai_services.create(FakeModelSource(model)).answer(
            USER, NEW, AiCreateAnswer(run_id=asked["runId"], answer="Ada's Bakery")
        )
    )
    again = result_of(first)
    assert again["status"] == "needs_input"
    assert again["question"]["question"].startswith('You already have a repository called "ada-s-bakery" on GitHub.')
    second = await collect(
        await ai_services.create(FakeModelSource(model)).answer(
            USER, NEW, AiCreateAnswer(run_id=asked["runId"], answer="Ada Buns")
        )
    )

    assert result_of(second)["site"]["repoName"] == "ada-buns"
    assert github.files["octocat/ada-s-bakery"]["index.html"].content == "mine"


async def test_a_site_with_its_repo_is_made_again_without_a_question(
    ai_services: AiServices, github: FakeGithubClient, site: str
) -> None:
    model = FakeModelClient(*replies())

    events = await collect(
        await ai_services.create(FakeModelSource(model)).create(
            USER, AiCreateRequest(project_id=PROJECT_ID, prompt=PROMPT)
        )
    )

    created = result_of(events)
    assert created["status"] == "created"
    assert github.files[REPO]["src/App.tsx"].content == APP
    # The files the site had stay (the hero from the fixture is replaced: the plan has the same path).
    assert ".makable/manifest.json" in github.files[REPO]


async def test_problems_the_checks_find_go_back_to_the_model(
    ai_services: AiServices, github: FakeGithubClient, site: str
) -> None:
    model = FakeModelClient(*replies(app=BROKEN_APP), fenced(APP))

    events = await collect(
        await ai_services.create(FakeModelSource(model)).create(
            USER, AiCreateRequest(project_id=PROJECT_ID, prompt=PROMPT)
        )
    )

    assert ("fix", "Fixing src/App.tsx") in [(e["stage"], e["message"]) for e in events if e["type"] == "progress"]
    fix = model.calls[4][0].text
    assert 'imports "./components/Heroes", but there\'s no such file' in fix
    assert github.files[REPO]["src/App.tsx"].content == APP
    assert "may still need fixing" not in result_of(events)["summary"]


async def test_problems_left_after_the_fixes_are_committed_and_said(
    ai_services: AiServices, github: FakeGithubClient, site: str
) -> None:
    model = FakeModelClient(*replies(app=BROKEN_APP), fenced(BROKEN_APP), fenced(BROKEN_APP))

    events = await collect(
        await ai_services.create(FakeModelSource(model)).create(
            USER, AiCreateRequest(project_id=PROJECT_ID, prompt=PROMPT)
        )
    )

    assert "may still need fixing" in result_of(events)["summary"]
    assert github.files[REPO]["src/App.tsx"].content == BROKEN_APP


async def test_a_reply_without_a_file_is_asked_for_again(ai_services: AiServices, site: str) -> None:
    model = FakeModelClient(PLAN, "Sure! Here are the colours: amber.", fenced(CSS, "css"), fenced(HERO), fenced(APP))

    events = await collect(
        await ai_services.create(FakeModelSource(model)).create(
            USER, AiCreateRequest(project_id=PROJECT_ID, prompt=PROMPT)
        )
    )

    assert result_of(events)["status"] == "created"
    assert "That reply was rejected: the reply has no code block" in model.calls[2][-1].text


async def test_a_creation_that_cant_be_planned_ends_with_an_error(
    ai_services: AiServices, github: FakeGithubClient, session: AsyncSession, site: str
) -> None:
    model = FakeModelClient("not json", '{"summary": "x", "files": []}')

    events = await collect(
        await ai_services.create(FakeModelSource(model)).create(
            USER, AiCreateRequest(project_id=PROJECT_ID, prompt=PROMPT)
        )
    )

    [error] = [e for e in events if e["type"] == "error"]
    assert error["error"]["code"] == "ai_create_failed"
    assert github.commits == []
    [record] = await records(session)
    assert (record.outcome, record.summary) == (AiEditOutcome.FAILED, "ai_create_failed")


class SlowModel:
    """Answers its replies in order, each after `delay` seconds; blocks forever when it runs out."""

    name = "slow-model"
    accepts_images = False

    def __init__(self, *replies: str, delay: float = 0.0) -> None:
        self.replies = list(replies)
        self.delay = delay
        self.blocked = asyncio.Event()

    async def complete(self, messages: list[ChatMessage]) -> str:
        await asyncio.sleep(self.delay)
        if not self.replies:
            self.blocked.set()
            await asyncio.Event().wait()
        return self.replies.pop(0)


async def test_pings_go_out_while_the_model_writes(ai_services: AiServices, site: str) -> None:
    model = SlowModel(*replies(), delay=0.12)

    events = await collect(
        await ai_services.create(FakeModelSource(model)).create(
            USER, AiCreateRequest(project_id=PROJECT_ID, prompt=PROMPT)
        )
    )

    assert {"type": "ping"} in events
    assert result_of(events)["status"] == "created"


async def test_closing_the_stream_stops_the_creation(
    ai_services: AiServices, github: FakeGithubClient, session: AsyncSession, site: str
) -> None:
    model = SlowModel(PLAN, fenced(CSS, "css"))
    events = await ai_services.create(FakeModelSource(model)).create(
        USER, AiCreateRequest(project_id=PROJECT_ID, prompt=PROMPT)
    )
    seen: list[dict[str, Any]] = []

    async def read() -> None:
        async for event in events:
            seen.append(event)

    reader = asyncio.create_task(read())
    await model.blocked.wait()
    reader.cancel()
    with pytest.raises(asyncio.CancelledError):
        await reader

    assert github.commits == []
    [record] = await records(session)
    assert record.outcome == AiEditOutcome.CANCELLED
    # The site is free again.
    again = FakeModelClient(*replies())
    created = await collect(
        await ai_services.create(FakeModelSource(again)).create(
            USER, AiCreateRequest(project_id=PROJECT_ID, prompt=PROMPT)
        )
    )
    assert result_of(created)["status"] == "created"


async def test_another_users_site_and_a_busy_site_are_refused_before_streaming(
    ai_services: AiServices, site: str
) -> None:
    service = ai_services.create(FakeModelSource(FakeModelClient()))

    with pytest.raises(SiteNotFoundError):
        await service.create(OTHER_USER, AiCreateRequest(project_id=PROJECT_ID, prompt=PROMPT))
    events = await service.create(USER, AiCreateRequest(project_id=PROJECT_ID, prompt=PROMPT))
    with pytest.raises(AiRunInProgressError):
        await service.create(USER, AiCreateRequest(project_id=PROJECT_ID, prompt=PROMPT))
    await events.aclose()  # type: ignore[attr-defined]


async def test_an_old_answer_is_superseded_and_start_over_forgets_the_creation(
    ai_services: AiServices, checkpointer: Any
) -> None:
    model = FakeModelClient(*replies())
    first = await asked_for_a_name(ai_services, model)
    second = await asked_for_a_name(ai_services, model)
    service = ai_services.create(FakeModelSource(model))

    with pytest.raises(AiRunSupersededError):
        await service.answer(USER, NEW, AiCreateAnswer(run_id=first["runId"], answer="Ada"))

    await ai_services.edit(FakeModelSource(model)).start_over(USER, NEW)

    assert await checkpointer.aget_tuple({"configurable": {"thread_id": f"{NEW}:create"}}) is None
    with pytest.raises(SiteNotFoundError):
        await service.answer(USER, NEW, AiCreateAnswer(run_id=second["runId"], answer="Ada"))
