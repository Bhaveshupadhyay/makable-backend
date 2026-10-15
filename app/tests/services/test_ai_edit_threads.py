"""The AI edit graph and its persistence: each route, the thread's turns across runs, ownership, Start over,
cancellation and the per-site run lock. Checkpoints go to an in-memory saver shared by the test's services."""

import asyncio
import json
from uuid import UUID, uuid4

import pytest
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.model import AiKeyInvalidError, ModelUnavailableError
from app.constants.ai_edit import MAX_HISTORY, AiEditOutcome
from app.models.ai_thread import AiEditRecord, AiThread
from app.schemas.ai_edit import AiEditTier1, AiEditTier2
from app.schemas.chat import ChatMessage
from app.services.ai_edit_service import (
    AiEditRejectedError,
    AiRunInProgressError,
    AiThreadNotFoundError,
)
from app.tests.ai_edit_fixtures import GOOD_REPLY, HERO_PATH, PROJECT_ID, MakeAiEditService, make_request
from app.tests.fakes import FakeModelClient, FakeModelSource

USER_ID = uuid4()
OTHER_USER_ID = uuid4()
CONFIG: RunnableConfig = {"configurable": {"thread_id": str(PROJECT_ID)}}


class BlockingModel:
    """Never answers until cancelled; says when it was called."""

    name = "blocking-model"

    def __init__(self) -> None:
        self.called = asyncio.Event()

    async def complete(self, messages: list[ChatMessage]) -> str:
        self.called.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


class FailingModel:
    name = "failing-model"

    def __init__(self, error: Exception) -> None:
        self.error = error

    async def complete(self, messages: list[ChatMessage]) -> str:
        raise self.error


async def records(session: AsyncSession) -> list[AiEditRecord]:
    session.expire_all()
    return list(await session.scalars(select(AiEditRecord).order_by(AiEditRecord.created_at)))


def bad_reply() -> str:
    return json.dumps({"summary": "x", "edits": [{"path": HERO_PATH, "search": "nope", "replace": "y"}]})


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
    replies: list[str],
    instruction: str,
    tier: int,
    outcome: AiEditOutcome,
    attempts: int,
    reply: str | None,
) -> None:
    model = FakeModelClient(*replies)

    result = await ai_edit_service(FakeModelSource(model)).edit(USER_ID, make_request(instruction=instruction))

    assert result.tier == tier
    assert len(model.calls) == attempts
    [record] = await records(session)
    assert (record.thread_id, record.user_id, record.instruction) == (PROJECT_ID, USER_ID, instruction)
    assert (record.tier, record.outcome, record.attempts) == (tier, outcome, attempts)
    assert record.model == ("fake-model" if attempts else None)
    assert record.selection is not None and record.selection["tag"] == "a"
    thread = await ai_edit_service(FakeModelSource(model)).thread(USER_ID, PROJECT_ID)
    [turn] = thread.turns
    assert (turn.instruction, turn.target, turn.outcome) == (instruction, 'Ada > <a> "Contact"', outcome)
    assert turn.reply == (reply or f"Not done: it needs a deeper edit ({result.reason}).")  # type: ignore[union-attr]


async def test_rejected_twice_is_recorded_but_not_added_to_the_chat(
    ai_edit_service: MakeAiEditService, session: AsyncSession
) -> None:
    service = ai_edit_service(FakeModelSource(FakeModelClient("not json", "still not json")))

    with pytest.raises(AiEditRejectedError):
        await service.edit(USER_ID, make_request())

    [record] = await records(session)
    assert (record.outcome, record.tier, record.attempts) == (AiEditOutcome.REJECTED, None, 2)
    assert record.summary is not None and "did not return JSON" in record.summary
    assert (await service.thread(USER_ID, PROJECT_ID)).turns == []


async def test_turns_accumulate_and_come_back_as_earlier_requests_instead_of_the_browsers(
    ai_edit_service: MakeAiEditService,
) -> None:
    model = FakeModelClient(GOOD_REPLY, GOOD_REPLY)
    browser_history = [{"instruction": "Something the browser made up", "target": None, "reply": "x"}]

    await ai_edit_service(FakeModelSource(model)).edit(USER_ID, make_request())
    # A new graph instance on the same saved thread: a closed tab, or a restarted server.
    await ai_edit_service(FakeModelSource(model)).edit(
        USER_ID, make_request(instruction="Make it bigger", history=browser_history)
    )

    second_prompt = model.calls[1][0].content
    assert '<earlier_requests>\n1. User (selected: Ada > <a> "Contact"): Make the contact button green\n' in (
        second_prompt
    )
    assert "Something the browser made up" not in second_prompt
    thread = await ai_edit_service(FakeModelSource(model)).thread(USER_ID, PROJECT_ID)
    assert [t.instruction for t in thread.turns] == ["Make the contact button green", "Make it bigger"]


async def test_only_the_last_turns_go_into_the_prompt(ai_edit_service: MakeAiEditService) -> None:
    model = FakeModelClient(*[GOOD_REPLY] * (MAX_HISTORY + 2))
    service = ai_edit_service(FakeModelSource(model))

    for i in range(MAX_HISTORY + 2):
        await service.edit(USER_ID, make_request(instruction=f"Request {i}"))

    last_prompt = model.calls[-1][0].content
    assert "Request 0\n" not in last_prompt
    assert f'{MAX_HISTORY}. User (selected: Ada > <a> "Contact"): Request {MAX_HISTORY}\n' in last_prompt


async def test_another_users_thread_is_not_found(ai_edit_service: MakeAiEditService, session: AsyncSession) -> None:
    model = FakeModelClient(GOOD_REPLY)
    service = ai_edit_service(FakeModelSource(model))
    await service.edit(USER_ID, make_request())

    with pytest.raises(AiThreadNotFoundError):
        await service.edit(OTHER_USER_ID, make_request())
    with pytest.raises(AiThreadNotFoundError):
        await service.thread(OTHER_USER_ID, PROJECT_ID)
    with pytest.raises(AiThreadNotFoundError):
        await service.start_over(OTHER_USER_ID, PROJECT_ID)
    assert len(model.calls) == 1
    assert len(await records(session)) == 1


async def test_a_site_without_a_thread_has_an_empty_chat(ai_edit_service: MakeAiEditService) -> None:
    thread = await ai_edit_service(FakeModelSource(FakeModelClient())).thread(USER_ID, uuid4())

    assert (thread.status, thread.turns) == ("idle", [])


async def test_start_over_deletes_the_checkpoints_requests_and_thread(
    ai_edit_service: MakeAiEditService, session: AsyncSession, checkpointer: InMemorySaver
) -> None:
    model = FakeModelClient(GOOD_REPLY, GOOD_REPLY)
    service = ai_edit_service(FakeModelSource(model))
    await service.edit(USER_ID, make_request())
    assert await checkpointer.aget_tuple(CONFIG) is not None

    await service.start_over(USER_ID, PROJECT_ID)

    assert await checkpointer.aget_tuple(CONFIG) is None
    assert await records(session) == []
    assert await session.get(AiThread, PROJECT_ID) is None
    assert (await service.thread(USER_ID, PROJECT_ID)).turns == []
    # The site can start again, as anyone's new thread would.
    await service.edit(USER_ID, make_request())
    assert "<earlier_requests>\n" not in model.calls[1][0].content


async def test_start_over_without_a_thread_does_nothing(ai_edit_service: MakeAiEditService) -> None:
    await ai_edit_service(FakeModelSource(FakeModelClient())).start_over(USER_ID, uuid4())


async def test_a_cancelled_run_is_recorded_and_doesnt_leak_into_the_next(
    ai_edit_service: MakeAiEditService, session: AsyncSession
) -> None:
    blocking = BlockingModel()
    run = asyncio.create_task(ai_edit_service(FakeModelSource(blocking)).edit(USER_ID, make_request()))
    await blocking.called.wait()

    run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run

    [record] = await records(session)
    assert (record.outcome, record.model, record.summary) == (AiEditOutcome.CANCELLED, "blocking-model", "stopped")
    model = FakeModelClient(GOOD_REPLY)
    result = await ai_edit_service(FakeModelSource(model)).edit(USER_ID, make_request(instruction="Try again"))
    assert isinstance(result, AiEditTier1)
    assert "<earlier_requests>\n" not in model.calls[0][0].content
    turns = (await ai_edit_service(FakeModelSource(model)).thread(USER_ID, PROJECT_ID)).turns
    assert [t.instruction for t in turns] == ["Try again"]


async def test_one_run_at_a_time_per_site(ai_edit_service: MakeAiEditService) -> None:
    blocking = BlockingModel()
    run = asyncio.create_task(ai_edit_service(FakeModelSource(blocking)).edit(USER_ID, make_request()))
    await blocking.called.wait()
    other = ai_edit_service(FakeModelSource(FakeModelClient(GOOD_REPLY)))

    with pytest.raises(AiRunInProgressError):
        await other.edit(USER_ID, make_request())
    with pytest.raises(AiRunInProgressError):
        await other.start_over(USER_ID, PROJECT_ID)
    # Another site isn't held up.
    assert isinstance(await other.edit(USER_ID, make_request(projectId=str(uuid4()))), AiEditTier1)

    run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run


async def test_a_failing_model_is_recorded_as_failed(ai_edit_service: MakeAiEditService, session: AsyncSession) -> None:
    service = ai_edit_service(FakeModelSource(FailingModel(ModelUnavailableError())))

    with pytest.raises(ModelUnavailableError):
        await service.edit(USER_ID, make_request())

    [record] = await records(session)
    assert (record.outcome, record.summary) == (AiEditOutcome.FAILED, ModelUnavailableError.code)
    assert (await service.thread(USER_ID, PROJECT_ID)).turns == []


async def test_a_refused_key_is_marked_and_the_request_recorded(
    ai_edit_service: MakeAiEditService, session: AsyncSession
) -> None:
    source = FakeModelSource(FailingModel(AiKeyInvalidError()))
    key_id = UUID("11111111-2222-3333-4444-555555555555")

    with pytest.raises(AiKeyInvalidError):
        await ai_edit_service(source).edit(USER_ID, make_request(model={"keyId": str(key_id), "model": "gpt-5"}))

    assert source.refused == [key_id]
    [record] = await records(session)
    assert (record.outcome, record.summary) == (AiEditOutcome.FAILED, "ai_key_invalid")


async def test_the_checkpoint_holds_no_file_contents(
    ai_edit_service: MakeAiEditService, checkpointer: InMemorySaver
) -> None:
    model = FakeModelClient(bad_reply(), GOOD_REPLY)
    await ai_edit_service(FakeModelSource(model)).edit(USER_ID, make_request())

    saved = await checkpointer.aget_tuple(CONFIG)

    assert saved is not None
    values = saved.checkpoint["channel_values"]
    assert "export function Hero()" not in json.dumps(values, default=str)
    assert values["messages"] == []
    # Plain values only: the serializer warns about (and will block) types it doesn't know, like our enums.
    assert type(values["result"]["outcome"]) is str
    assert {type(v) for t in values["turns"] for v in t.values()} <= {str, type(None)}


async def test_tier_2_by_rule_needs_no_model(ai_edit_service: MakeAiEditService) -> None:
    class NoModel(FakeModelSource):
        async def model_for(self, user_id: UUID, choice: object) -> FakeModelClient:
            raise AssertionError("no model should be picked")

    result = await ai_edit_service(NoModel(FakeModelClient())).edit(USER_ID, make_request(instruction="Add a page"))

    assert isinstance(result, AiEditTier2)
