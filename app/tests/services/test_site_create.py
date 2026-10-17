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

from app.agents.create_site.nodes.ask_name import NAME_QUESTION
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
# The questions about the site: none to ask.
READY = json.dumps({"ready": True})
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
    return [READY, PLAN, fenced(CSS, "css"), fenced(HERO), fenced(app)]


async def collect(events: AsyncIterator[dict[str, Any]]) -> list[dict[str, Any]]:
    return [event async for event in events]


def result_of(events: list[dict[str, Any]]) -> dict[str, Any]:
    [result] = [e for e in events if e["type"] == "result"]
    data: dict[str, Any] = result["data"]
    return data


async def records(session: AsyncSession) -> list[AiEditRecord]:
    session.expire_all()
    return list(await session.scalars(select(AiEditRecord).order_by(AiEditRecord.created_at)))


async def started(ai_services: AiServices, model: FakeModelClient) -> dict[str, Any]:
    events = await collect(
        await ai_services.create(FakeModelSource(model)).create(USER, AiCreateRequest(project_id=NEW, prompt=PROMPT))
    )
    return result_of(events)


async def test_a_site_is_created_from_a_prompt(
    ai_services: AiServices, github: FakeGithubClient, session: AsyncSession
) -> None:
    model = FakeModelClient(*replies())

    asked = await started(ai_services, model)

    assert asked["status"] == "needs_input"
    assert asked["question"]["kind"] == "project_name"
    assert asked["question"]["options"] == ["Ada's Bakery"]
    assert github.repos == {}
    thread = await ai_services.edit(FakeModelSource(model)).thread(USER, NEW)
    assert (thread.status, thread.run and thread.run.mode) == ("needs_input", "create")
    # What it did before it asked stays in the chat.
    assert thread.run is not None and thread.run.steps == ["Thinking about your site"]

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
    assert (named.question, named.answer, named.steps) == (
        "What should we call this site? It's also the name of its repository on your GitHub.",
        "Ada's Bakery",
        ["Thinking about your site"],
    )
    # The build's steps stay with its reply.
    assert turn.steps == [message for _, message in progress]
    [record] = await records(session)
    assert (record.outcome, record.attempts, record.model) == (AiEditOutcome.APPLIED, 5, "fake-model")
    # The questions about the site (none), the plan, then each file with the files written before it.
    assert "<user_answers>\n" not in model.calls[1][0].text
    assert '<file path="src/index.css">\n' + CSS in model.calls[3][0].text
    assert "Write src/App.tsx: the page" in model.calls[4][0].text


async def test_a_taken_name_is_asked_again(ai_services: AiServices, github: FakeGithubClient) -> None:
    github.add_repo("ada-s-bakery", private=False, files={"index.html": "mine"})
    # The questions about the site are asked once; a typed name (not an option) is read by the model.
    model = FakeModelClient(READY, json.dumps({"name": "Ada Buns"}), *replies()[1:])
    asked = await started(ai_services, model)

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


async def answer_with(
    ai_services: AiServices,
    model: FakeModelClient,
    run_id: str,
    text: str,
    *,
    chat: bool = False,
    project_id: UUID = NEW,
) -> dict[str, Any]:
    return result_of(
        await collect(
            await ai_services.create(FakeModelSource(model)).answer(
                USER, project_id, AiCreateAnswer(run_id=UUID(run_id), answer=text, chat=chat)
            )
        )
    )


async def test_an_answer_that_isnt_a_name_is_answered_and_the_name_asked_again(
    ai_services: AiServices, github: FakeGithubClient
) -> None:
    chat = {
        "reply": "Hi! Any name works: it's your site's title and its repository's name on your GitHub. "
        "What should we call it?",
        "suggestions": ["Ada's Bakery", "Sweet Ada"],
    }
    model = FakeModelClient(READY, json.dumps(chat), *replies()[1:])
    asked = await started(ai_services, model)

    again = await answer_with(ai_services, model, asked["runId"], "hi, what should I call it?")

    assert again["status"] == "needs_input"
    assert again["runId"] == asked["runId"]
    assert again["question"] == {
        "question": chat["reply"],
        "options": ["Ada's Bakery", "Sweet Ada"],
        "allowFreeText": True,
        "kind": "project_name",
    }
    assert github.repos == {}
    prompt = model.calls[1][0].text
    assert "<user_answer>\nhi, what should I call it?\n</user_answer>" in prompt
    assert "<options_shown>\n- Ada's Bakery\n</options_shown>" in prompt
    thread = await ai_services.edit(FakeModelSource(model)).thread(USER, NEW)
    assert (thread.status, thread.run and thread.run.question and thread.run.question.question) == (
        "needs_input",
        chat["reply"],
    )

    # A clicked suggestion is the name, without asking the model.
    created = await answer_with(ai_services, model, asked["runId"], "sweet ada")

    assert (created["site"]["name"], created["site"]["repoName"]) == ("Sweet Ada", "sweet-ada")
    assert len(model.calls) == 6
    thread = await ai_services.edit(FakeModelSource(model)).thread(USER, NEW)
    [turn] = thread.turns
    assert [(q.question, q.answer) for q in turn.questions] == [
        (NAME_QUESTION, "hi, what should I call it?"),
        (chat["reply"], "sweet ada"),
    ]


async def test_a_name_in_a_sentence_is_taken_out_of_it(ai_services: AiServices, github: FakeGithubClient) -> None:
    model = FakeModelClient(READY, json.dumps({"name": "Crumb & Co"}), *replies()[1:])
    asked = await started(ai_services, model)

    created = await answer_with(ai_services, model, asked["runId"], "let's call it crumb & co please")

    assert (created["site"]["name"], created["site"]["repoName"]) == ("Crumb & Co", "crumb-co")


async def test_a_name_the_user_didnt_give_is_asked_about_first(
    ai_services: AiServices, github: FakeGithubClient
) -> None:
    model = FakeModelClient(READY, json.dumps({"name": "Golden Crust"}), *replies()[1:])
    asked = await started(ai_services, model)

    again = await answer_with(ai_services, model, asked["runId"], "you choose")

    assert again["status"] == "needs_input"
    assert (again["question"]["question"], again["question"]["options"]) == (
        'Should we call it "Golden Crust"?',
        ["Golden Crust"],
    )
    assert github.repos == {}


async def test_an_answer_the_model_cant_read_asks_again(ai_services: AiServices, github: FakeGithubClient) -> None:
    model = FakeModelClient(READY, "no idea", '{"reply": ""}')
    asked = await started(ai_services, model)

    again = await answer_with(ai_services, model, asked["runId"], "hmm")

    assert again["question"]["question"].startswith("Sorry, I didn't catch a name there. What should we call")
    assert again["question"]["options"] == ["Ada's Bakery"]
    assert "That reply was rejected: The model did not return JSON" in model.calls[2][2].text
    assert github.repos == {}


async def test_chatting_about_the_name_question_is_only_replied_to(
    ai_services: AiServices, github: FakeGithubClient
) -> None:
    chat = {"reply": "Ada's Bakery is clear and simple. What should we call it?", "suggestions": ["Ada's Bakery"]}
    # A name the model gives back to a chat is asked about, not taken.
    model = FakeModelClient(READY, json.dumps(chat), json.dumps({"name": "Ada's Bakery"}), *replies()[1:])
    asked = await started(ai_services, model)

    # Even text that matches an option is a message when the user chose to chat.
    again = await answer_with(ai_services, model, asked["runId"], "Ada's Bakery", chat=True)

    assert (again["question"]["question"], again["question"]["options"]) == (chat["reply"], ["Ada's Bakery"])
    assert "<user_message>\nAda's Bakery\n</user_message>" in model.calls[1][0].text
    assert "Instead of answering, the user chose to talk about the question." in model.calls[1][0].text
    confirm = await answer_with(ai_services, model, asked["runId"], "is that a good name?", chat=True)
    assert confirm["question"]["question"] == 'Should we call it "Ada\'s Bakery"?'
    assert github.repos == {}
    thread = await ai_services.edit(FakeModelSource(model)).thread(USER, NEW)
    assert thread.run is not None
    assert [(q.answer, q.chat) for q in thread.run.questions] == [
        ("Ada's Bakery", True),
        ("is that a good name?", True),
    ]

    created = await answer_with(ai_services, model, asked["runId"], "Ada's Bakery")

    assert created["site"]["repoName"] == "ada-s-bakery"


KIND = json.dumps(
    {
        "ask": {
            "question": "What kind of site is it?",
            "options": ["A menu and opening hours page", "An online shop for orders"],
            "allowFreeText": False,
        }
    }
)


async def test_questions_about_the_site_come_first_then_the_name_with_names_that_fit(
    ai_services: AiServices, github: FakeGithubClient
) -> None:
    look = json.dumps({"ask": {"question": "Which look do you want?", "options": ["Warm and cosy", "Clean"]}})
    ready = json.dumps({"ready": True, "names": ["Ada's Cakes", "Crumb & Co", "Ada's Cakes"]})
    model = FakeModelClient(KIND, look, ready, *replies()[1:])

    kind = await started(ai_services, model)

    assert kind["status"] == "needs_input"
    assert kind["question"] == {
        "question": "What kind of site is it?",
        "options": ["A menu and opening hours page", "An online shop for orders"],
        "allowFreeText": True,
        "kind": "site_brief",
    }
    assert github.repos == {}
    first = model.calls[0][0].text
    assert "<asked>\n(nothing yet)\n</asked>" in first
    assert "<site_name>" not in first
    thread = await ai_services.edit(FakeModelSource(model)).thread(USER, NEW)
    assert thread.run is not None and thread.run.question is not None
    assert (thread.run.question.kind, thread.run.questions) == ("site_brief", [])

    again = await answer_with(ai_services, model, kind["runId"], "An online shop for orders")

    assert again["question"]["question"] == "Which look do you want?"
    assert "Q: What kind of site is it?\nA: An online shop for orders" in model.calls[1][0].text
    assert "You have asked 1 of at most 3 questions." in model.calls[1][0].text

    named = await answer_with(ai_services, model, kind["runId"], "Warm and cosy")

    assert named["question"] == {
        "question": NAME_QUESTION,
        "options": ["Ada's Cakes", "Crumb & Co"],
        "allowFreeText": True,
        "kind": "project_name",
    }
    assert github.repos == {}

    created = await answer_with(ai_services, model, kind["runId"], "Crumb & Co")

    assert (created["site"]["name"], created["site"]["repoName"]) == ("Crumb & Co", "crumb-co")
    # A clicked name isn't read by the model.
    assert len(model.calls) == 3 + 4
    answers = (
        "<user_answers>\nQ: What kind of site is it?\nA: An online shop for orders\n"
        "Q: Which look do you want?\nA: Warm and cosy\n</user_answers>"
    )
    # The plan and every file are told the answers.
    assert answers in model.calls[3][0].text
    assert answers in model.calls[4][0].text
    thread = await ai_services.edit(FakeModelSource(model)).thread(USER, NEW)
    [turn] = thread.turns
    asked_then = [(q.question, q.steps) for q in turn.questions]
    assert asked_then == [
        ("What kind of site is it?", ["Thinking about your site"]),
        ("Which look do you want?", ["Thinking about your site"]),
        (NAME_QUESTION, ["Thinking about your site"]),
    ]


async def test_chatting_about_a_question_about_the_site_is_replied_to_in_it(
    ai_services: AiServices, github: FakeGithubClient
) -> None:
    which = "An online shop takes orders; a menu page shows what you bake. Which one is it?"
    reply = json.dumps({"ask": {"question": which, "options": ["Online shop", "Menu page"]}})
    model = FakeModelClient(KIND, reply, *replies())
    asked = await started(ai_services, model)

    again = await answer_with(ai_services, model, asked["runId"], "what's the difference?", chat=True)

    assert (again["question"]["question"], again["question"]["kind"]) == (which, "site_brief")
    prompt = model.calls[1][0].text
    assert "Q: What kind of site is it?\nMessage about it (not an answer): what's the difference?" in prompt
    # A message isn't an answer.
    assert "You have asked 0 of at most 3 questions." in prompt

    named = await answer_with(ai_services, model, asked["runId"], "Online shop")
    assert named["question"]["kind"] == "project_name"
    created = await answer_with(ai_services, model, asked["runId"], "Ada's Bakery")

    plan = model.calls[3][0].text
    assert f"<user_answers>\nQ: {which}\nA: Online shop\n</user_answers>" in plan
    assert "what's the difference?" not in plan
    assert created["status"] == "created"


async def test_after_three_questions_the_model_only_suggests_names(
    ai_services: AiServices, github: FakeGithubClient
) -> None:
    ready = json.dumps({"ready": True, "names": ["Ada's Cakes"]})
    model = FakeModelClient(KIND, KIND, KIND, ready)
    asked = await started(ai_services, model)

    for _ in range(3):
        last = await answer_with(ai_services, model, asked["runId"], "A menu")

    assert (last["question"]["kind"], last["question"]["options"]) == ("project_name", ["Ada's Cakes"])
    assert "you know enough now" in model.calls[3][0].text
    assert '"ask"' not in model.calls[3][0].text


async def test_at_most_three_questions_are_asked_about_the_site(
    ai_services: AiServices, github: FakeGithubClient, site: str
) -> None:
    # A site with its repo has its name: only the questions are asked, and no names suggested after them.
    model = FakeModelClient(KIND, KIND, KIND, *replies()[1:])
    asked = result_of(
        await collect(
            await ai_services.create(FakeModelSource(model)).create(
                USER, AiCreateRequest(project_id=PROJECT_ID, prompt=PROMPT)
            )
        )
    )
    assert asked["question"]["kind"] == "site_brief"

    for _ in range(2):
        again = await answer_with(ai_services, model, asked["runId"], "A menu", project_id=PROJECT_ID)
        assert again["status"] == "needs_input"
    created = await answer_with(ai_services, model, asked["runId"], "A menu", project_id=PROJECT_ID)

    assert created["status"] == "created"
    assert len(model.calls) == 3 + 4


async def test_a_model_that_cant_ask_doesnt_stop_the_creation(
    ai_services: AiServices, github: FakeGithubClient
) -> None:
    model = FakeModelClient("no idea", '{"ask": {}}', *replies()[1:])
    asked = await started(ai_services, model)

    # The name is asked, with one from the prompt.
    assert (asked["question"]["kind"], asked["question"]["options"]) == ("project_name", ["Ada's Bakery"])
    created = await answer_with(ai_services, model, asked["runId"], "Ada's Bakery")

    assert created["status"] == "created"
    assert "That reply was rejected: The model did not return JSON" in model.calls[1][2].text


async def test_a_message_the_model_cant_reply_to_asks_the_question_again(
    ai_services: AiServices, github: FakeGithubClient
) -> None:
    model = FakeModelClient(KIND, "no idea", "still no idea")
    asked = await started(ai_services, model)

    again = await answer_with(ai_services, model, asked["runId"], "hmm?", chat=True)

    assert again["question"]["question"] == "What kind of site is it?"
    assert github.repos == {}


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
    fix = model.calls[5][0].text
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
    no_file = "Sure! Here are the colours: amber."
    model = FakeModelClient(READY, PLAN, no_file, fenced(CSS, "css"), fenced(HERO), fenced(APP))

    events = await collect(
        await ai_services.create(FakeModelSource(model)).create(
            USER, AiCreateRequest(project_id=PROJECT_ID, prompt=PROMPT)
        )
    )

    assert result_of(events)["status"] == "created"
    assert "That reply was rejected: the reply has no code block" in model.calls[3][-1].text


async def test_a_creation_that_cant_be_planned_ends_with_an_error(
    ai_services: AiServices, github: FakeGithubClient, session: AsyncSession, site: str
) -> None:
    model = FakeModelClient(READY, "not json", '{"summary": "x", "files": []}')

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
    model = SlowModel(READY, PLAN, fenced(CSS, "css"))
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


async def test_stop_during_the_commit_still_commits_and_the_chat_shows_it(
    ai_services: AiServices, github: FakeGithubClient, session: AsyncSession, site: str
) -> None:
    github.commit_delay = 0.2
    # Problems are left after the fixes, so the commit's summary says so.
    model = FakeModelClient(*replies(app=BROKEN_APP), fenced(BROKEN_APP), fenced(BROKEN_APP))
    events = await ai_services.create(FakeModelSource(model)).create(
        USER, AiCreateRequest(project_id=PROJECT_ID, prompt=PROMPT)
    )

    async def read() -> None:
        async for _ in events:
            pass

    reader = asyncio.create_task(read())
    await github.commit_started.wait()
    await asyncio.sleep(0.05)  # Into the commit.
    reader.cancel()
    with pytest.raises(asyncio.CancelledError):
        await reader

    head = github.heads[REPO].commit_sha
    [record] = await records(session)
    assert (record.outcome, record.commit_sha, record.attempts) == (AiEditOutcome.APPLIED, head, 7)
    assert record.model == model.name
    [turn] = (await ai_services.edit(FakeModelSource(model)).thread(USER, PROJECT_ID)).turns
    assert (turn.outcome, turn.commit_sha) == (AiEditOutcome.APPLIED, head)
    assert turn.reply.startswith(json.loads(PLAN)["summary"])
    assert turn.steps[-2:] == ["Checking the code", "Saving the site to your GitHub"]
    assert "may still need fixing" in turn.reply


async def test_another_users_site_and_a_busy_site_are_refused_before_streaming(
    ai_services: AiServices, site: str
) -> None:
    service = ai_services.create(FakeModelSource(FakeModelClient()))

    with pytest.raises(SiteNotFoundError):
        await service.create(OTHER_USER, AiCreateRequest(project_id=PROJECT_ID, prompt=PROMPT))
    events = await service.create(USER, AiCreateRequest(project_id=PROJECT_ID, prompt=PROMPT))
    with pytest.raises(AiRunInProgressError):
        await service.create(USER, AiCreateRequest(project_id=PROJECT_ID, prompt=PROMPT))
    await events.aclose()


async def test_events_closed_without_being_read_free_the_site(ai_services: AiServices, site: str) -> None:
    service = ai_services.create(FakeModelSource(FakeModelClient(*replies())))
    events = await service.create(USER, AiCreateRequest(project_id=PROJECT_ID, prompt=PROMPT))

    await events.aclose()

    created = await collect(await service.create(USER, AiCreateRequest(project_id=PROJECT_ID, prompt=PROMPT)))
    assert result_of(created)["status"] == "created"


async def test_an_old_answer_is_superseded_and_start_over_forgets_the_creation(
    ai_services: AiServices, checkpointer: Any
) -> None:
    model = FakeModelClient(*replies())
    first = await started(ai_services, model)
    second = await started(ai_services, model)
    service = ai_services.create(FakeModelSource(model))

    with pytest.raises(AiRunSupersededError):
        await service.answer(USER, NEW, AiCreateAnswer(run_id=first["runId"], answer="Ada"))

    await ai_services.edit(FakeModelSource(model)).start_over(USER, NEW)

    assert await checkpointer.aget_tuple({"configurable": {"thread_id": f"{NEW}:create"}}) is None
    with pytest.raises(SiteNotFoundError):
        await service.answer(USER, NEW, AiCreateAnswer(run_id=second["runId"], answer="Ada"))
