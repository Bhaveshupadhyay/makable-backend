"""The AI edit agent and its persistence: each route, the thread's turns across runs, ownership, Start over,
cancellation (before and during the commit) and the per-site run lock. Checkpoints go to an in-memory saver shared by
the test's services."""

import asyncio
import json
from uuid import UUID

import pytest
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.model import AiKeyInvalidError, ModelUnavailableError
from app.constants.ai_edit import MAX_HISTORY, AiEditOutcome
from app.constants.auth import Role
from app.models.site import AiEditRecord, Site
from app.schemas.ai_edit import AiEditApplied, AiEditAwaitingResult, AiEditEscalated, AiEditPreviewReport
from app.schemas.chat import ChatMessage
from app.schemas.user import UserRead
from app.services.ai_edit_service import AiEditRejectedError, AiRunInProgressError
from app.services.site_service import SiteNotFoundError
from app.tests.ai_edit_fixtures import (
    BASE_COMMIT,
    GOOD_REPLY,
    GREEN_HERO,
    HERO_PATH,
    PROJECT_ID,
    REPO,
    USER,
    MakeAiEditService,
    confirmed,
    make_request,
)
from app.tests.fakes import FakeGithubClient, FakeModelClient, FakeModelSource

OTHER_USER = UserRead(
    id=UUID("9a8b7c6d-5e4f-4a3b-9c2d-1e0f9a8b7c6d"),
    login="hubot",
    name=None,
    avatar_url="https://x/h.png",
    role=Role.USER,
)
CONFIG: RunnableConfig = {"configurable": {"thread_id": str(PROJECT_ID)}}


class BlockingModel:
    """Never answers until cancelled; says when it was called."""

    name = "blocking-model"
    accepts_images = False

    def __init__(self) -> None:
        self.called = asyncio.Event()

    async def complete(self, messages: list[ChatMessage]) -> str:
        self.called.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


class FailingModel:
    name = "failing-model"
    accepts_images = False

    def __init__(self, error: Exception) -> None:
        self.error = error

    async def complete(self, messages: list[ChatMessage]) -> str:
        raise self.error


async def records(session: AsyncSession) -> list[AiEditRecord]:
    session.expire_all()
    return list(await session.scalars(select(AiEditRecord).order_by(AiEditRecord.created_at)))


def bad_reply() -> str:
    return json.dumps({"summary": "x", "edits": [{"path": HERO_PATH, "search": "nope", "replace": "y"}]})


def note_reply(n: int) -> str:
    """An edit that always applies: a comment added to the stylesheet."""
    edit = {
        "path": "src/index.css",
        "search": ":root { --bg: #fff; }",
        "replace": f":root {{ --bg: #fff; }}\n/* {n} */",
    }
    return json.dumps({"summary": f"Noted {n}.", "edits": [edit]})


@pytest.mark.parametrize(
    ("replies", "instruction", "tier", "outcome", "attempts", "reply"),
    [
        ([GOOD_REPLY], "Make the contact button green", 1, AiEditOutcome.APPLIED, 1, "Made it green."),
        ([bad_reply(), GOOD_REPLY], "Make the contact button green", 1, AiEditOutcome.APPLIED, 2, "Made it green."),
        (
            ['{"escalate": "Needs the navbar."}'],
            "Make the contact button green",
            2,
            AiEditOutcome.ESCALATED,
            1,
            "Not done: it needs a deeper edit (Needs the navbar.).",
        ),
        ([], "Install framer motion", 2, AiEditOutcome.ESCALATED, 0, None),
    ],
    ids=["ok", "repaired", "escalated", "tier-2-rule"],
)
async def test_each_route_records_the_request_and_adds_a_turn(
    ai_edit_service: MakeAiEditService,
    session: AsyncSession,
    github: FakeGithubClient,
    replies: list[str],
    instruction: str,
    tier: int,
    outcome: AiEditOutcome,
    attempts: int,
    reply: str | None,
) -> None:
    model = FakeModelClient(*replies)

    result = await confirmed(ai_edit_service(FakeModelSource(model)), make_request(instruction=instruction))

    assert len(model.calls) == attempts
    [record] = await records(session)
    assert (record.site_id, record.user_id, record.instruction) == (PROJECT_ID, USER.id, instruction)
    assert (record.tier, record.outcome, record.attempts) == (tier, outcome, attempts)
    assert record.model == ("fake-model" if attempts else None)
    assert record.selection is not None and record.selection["tag"] == "a"
    applied = outcome == AiEditOutcome.APPLIED
    assert record.commit_sha == (github.heads[REPO].commit_sha if applied else None)
    thread = await ai_edit_service(FakeModelSource(model)).thread(USER, PROJECT_ID)
    [turn] = thread.turns
    assert (turn.instruction, turn.target, turn.outcome) == (instruction, 'Ada > <a> "Contact"', outcome)
    if isinstance(result, AiEditEscalated):
        assert turn.reply == (reply or f"Not done: it needs a deeper edit ({result.reason}).")
    else:
        assert isinstance(result, AiEditApplied)
        assert turn.reply == reply


async def test_rejected_twice_is_recorded_but_not_added_to_the_chat(
    ai_edit_service: MakeAiEditService, session: AsyncSession
) -> None:
    service = ai_edit_service(FakeModelSource(FakeModelClient("not json", "still not json")))

    with pytest.raises(AiEditRejectedError):
        await confirmed(service, make_request())

    [record] = await records(session)
    assert (record.outcome, record.tier, record.attempts) == (AiEditOutcome.REJECTED, None, 2)
    assert record.summary is not None and "did not return JSON" in record.summary
    assert (await service.thread(USER, PROJECT_ID)).turns == []


async def test_turns_accumulate_and_come_back_as_earlier_requests(
    ai_edit_service: MakeAiEditService, github: FakeGithubClient
) -> None:
    model = FakeModelClient(GOOD_REPLY, note_reply(1))

    await confirmed(ai_edit_service(FakeModelSource(model)), make_request())
    # A new graph instance on the same saved thread: a closed tab, or a restarted server.
    head = github.heads[REPO].commit_sha
    await confirmed(
        ai_edit_service(FakeModelSource(model)), make_request(instruction="Make it bigger", baseCommit=head)
    )

    second_prompt = model.calls[1][0].text
    assert '<earlier_requests>\n1. User (selected: Ada > <a> "Contact"): Make the contact button green\n' in (
        second_prompt
    )
    # The second request reads the site as the first one left it.
    assert 'className="bg-emerald-600"' in second_prompt
    thread = await ai_edit_service(FakeModelSource(model)).thread(USER, PROJECT_ID)
    assert [t.instruction for t in thread.turns] == ["Make the contact button green", "Make it bigger"]


async def test_only_the_last_turns_go_into_the_prompt(
    ai_edit_service: MakeAiEditService, github: FakeGithubClient
) -> None:
    model = FakeModelClient(*[note_reply(i) for i in range(MAX_HISTORY + 2)])
    service = ai_edit_service(FakeModelSource(model))

    for i in range(MAX_HISTORY + 2):
        head = github.heads[REPO].commit_sha
        await confirmed(service, make_request(instruction=f"Request {i}", baseCommit=head))

    last_prompt = model.calls[-1][0].text
    assert "Request 0\n" not in last_prompt
    assert f'{MAX_HISTORY}. User (selected: Ada > <a> "Contact"): Request {MAX_HISTORY}\n' in last_prompt


async def test_another_users_site_is_not_found(ai_edit_service: MakeAiEditService, session: AsyncSession) -> None:
    model = FakeModelClient(GOOD_REPLY)
    service = ai_edit_service(FakeModelSource(model))
    await confirmed(service, make_request())

    with pytest.raises(SiteNotFoundError):
        await service.edit(OTHER_USER, make_request())
    with pytest.raises(SiteNotFoundError):
        await service.thread(OTHER_USER, PROJECT_ID)
    with pytest.raises(SiteNotFoundError):
        await service.start_over(OTHER_USER, PROJECT_ID)
    assert len(model.calls) == 1
    assert len(await records(session)) == 1


async def test_a_site_without_a_thread_has_an_empty_chat(ai_edit_service: MakeAiEditService) -> None:
    service = ai_edit_service(FakeModelSource(FakeModelClient()))

    assert (await service.thread(USER, PROJECT_ID)).turns == []
    unknown = await service.thread(USER, UUID("22222222-2222-4222-8222-222222222222"))
    assert (unknown.status, unknown.turns) == ("idle", [])


async def test_start_over_forgets_the_site_but_leaves_its_repo(
    ai_edit_service: MakeAiEditService, session: AsyncSession, checkpointer: InMemorySaver, github: FakeGithubClient
) -> None:
    service = ai_edit_service(FakeModelSource(FakeModelClient(GOOD_REPLY)))
    await confirmed(service, make_request())
    assert await checkpointer.aget_tuple(CONFIG) is not None

    await service.start_over(USER, PROJECT_ID)

    assert await checkpointer.aget_tuple(CONFIG) is None
    assert await records(session) == []
    session.expire_all()
    assert await session.get(Site, PROJECT_ID) is None
    assert (await service.thread(USER, PROJECT_ID)).turns == []
    assert github.files[REPO][HERO_PATH].content == GREEN_HERO
    # Nothing to forget is fine.
    await service.start_over(USER, PROJECT_ID)


async def test_a_cancelled_run_is_recorded_and_doesnt_leak_into_the_next(
    ai_edit_service: MakeAiEditService, session: AsyncSession, github: FakeGithubClient
) -> None:
    blocking = BlockingModel()
    run = asyncio.create_task(ai_edit_service(FakeModelSource(blocking)).edit(USER, make_request()))
    await blocking.called.wait()

    run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run

    [record] = await records(session)
    assert (record.outcome, record.model, record.summary) == (AiEditOutcome.CANCELLED, "blocking-model", "stopped")
    assert github.commits == []
    model = FakeModelClient(GOOD_REPLY)
    result = await confirmed(ai_edit_service(FakeModelSource(model)), make_request(instruction="Try again"))
    assert isinstance(result, AiEditApplied)
    assert "<earlier_requests>\n" not in model.calls[0][0].text
    turns = (await ai_edit_service(FakeModelSource(model)).thread(USER, PROJECT_ID)).turns
    assert [t.instruction for t in turns] == ["Try again"]


async def test_stop_during_the_commit_still_commits_and_the_chat_shows_it(
    ai_edit_service: MakeAiEditService, session: AsyncSession, github: FakeGithubClient
) -> None:
    github.commit_delay = 0.2
    service = ai_edit_service(FakeModelSource(FakeModelClient(GOOD_REPLY)))
    waiting = await service.edit(USER, make_request())
    assert isinstance(waiting, AiEditAwaitingResult)
    report = AiEditPreviewReport(run_id=waiting.run_id, ok=True)
    run = asyncio.create_task(service.report(USER, PROJECT_ID, report))
    await github.commit_started.wait()
    await asyncio.sleep(0.05)  # Into the commit.

    run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run

    head = github.heads[REPO].commit_sha
    assert head != BASE_COMMIT
    assert github.files[REPO][HERO_PATH].content == GREEN_HERO
    [record] = await records(session)
    assert (record.outcome, record.commit_sha) == (AiEditOutcome.APPLIED, head)
    [turn] = (await service.thread(USER, PROJECT_ID)).turns
    assert (turn.outcome, turn.reply) == (AiEditOutcome.APPLIED, "Made it green.")


async def test_one_run_at_a_time_per_site(ai_edit_service: MakeAiEditService) -> None:
    blocking = BlockingModel()
    run = asyncio.create_task(ai_edit_service(FakeModelSource(blocking)).edit(USER, make_request()))
    await blocking.called.wait()
    other = ai_edit_service(FakeModelSource(FakeModelClient(GOOD_REPLY)))

    with pytest.raises(AiRunInProgressError):
        await confirmed(other, make_request())
    with pytest.raises(AiRunInProgressError):
        await other.start_over(USER, PROJECT_ID)
    # Another site isn't held up (this one isn't the user's, so it's not found rather than busy).
    with pytest.raises(SiteNotFoundError):
        await confirmed(other, make_request(projectId="33333333-3333-4333-8333-333333333333"))

    run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run


async def test_a_failing_model_is_recorded_as_failed(ai_edit_service: MakeAiEditService, session: AsyncSession) -> None:
    service = ai_edit_service(FakeModelSource(FailingModel(ModelUnavailableError())))

    with pytest.raises(ModelUnavailableError):
        await confirmed(service, make_request())

    [record] = await records(session)
    assert (record.outcome, record.summary) == (AiEditOutcome.FAILED, ModelUnavailableError.code)
    assert (await service.thread(USER, PROJECT_ID)).turns == []


async def test_a_refused_key_is_marked_and_the_request_recorded(
    ai_edit_service: MakeAiEditService, session: AsyncSession
) -> None:
    source = FakeModelSource(FailingModel(AiKeyInvalidError()))
    key_id = UUID("11111111-2222-3333-4444-555555555555")

    with pytest.raises(AiKeyInvalidError):
        await ai_edit_service(source).edit(USER, make_request(model={"keyId": str(key_id), "model": "gpt-5"}))

    assert source.refused == [key_id]
    [record] = await records(session)
    assert (record.outcome, record.summary) == (AiEditOutcome.FAILED, "ai_key_invalid")


async def test_the_checkpoint_holds_no_file_contents(
    ai_edit_service: MakeAiEditService, checkpointer: InMemorySaver
) -> None:
    ask = '{"needContext": {"path": "src/App.tsx"}}'
    model = FakeModelClient(ask, bad_reply(), GOOD_REPLY)
    await confirmed(ai_edit_service(FakeModelSource(model)), make_request())

    saved = await checkpointer.aget_tuple(CONFIG)

    assert saved is not None
    values = saved.checkpoint["channel_values"]
    assert "export function" not in json.dumps(values, default=str)
    assert values["messages"] == []
    assert values["result"]["outcome"] == AiEditOutcome.APPLIED
    assert values["context"][0] == {"path": HERO_PATH, "start": 1, "end": 8}
    # Plain values only: the serializer warns about (and will block) types it doesn't know, like our enums.
    assert type(values["result"]["outcome"]) is str
    assert {type(v) for t in values["turns"] for k, v in t.items() if k != "questions"} <= {str, type(None)}
    assert values["turns"][0]["questions"] == []
