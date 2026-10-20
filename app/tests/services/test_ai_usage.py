"""What an AI request took (tokens, time): recorded for every request, and shown in the answer and the chat only for
requests with the user's own key."""

import json
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.model import ModelReply, TokenUsage
from app.constants.ai_edit import AiEditOutcome
from app.models.site import AiEditRecord
from app.schemas.ai_create import AiCreateAnswer, AiCreateRequest
from app.schemas.ai_edit import AiEditAnswer, AiEditApplied, AiEditNeedsInput
from app.services.ai_edit_service import AiEditRejectedError
from app.tests.ai_edit_fixtures import BASE_COMMIT, GOOD_REPLY, PROJECT_ID, USER, MakeAiEditService, confirm, confirmed
from app.tests.ai_edit_fixtures import make_request as edit_request
from app.tests.conftest import AiServices
from app.tests.fakes import FakeModelClient, FakeModelSource
from app.tests.services.test_ai_questions import ASK
from app.tests.services.test_site_create import NEW, PROMPT, collect, replies, result_of

# The user's own key's model.
OWN_MODEL = {"keyId": str(uuid4()), "model": "gpt-5"}


def reply(text: str, prompt: int = 1000, completion: int = 200, **details: Any) -> ModelReply:
    return ModelReply(text, TokenUsage(prompt, completion, **details), seconds=1.5)


def make_request(**overrides: Any) -> Any:
    return edit_request(**{"model": OWN_MODEL, **overrides})


async def records(session: AsyncSession) -> list[AiEditRecord]:
    session.expire_all()
    return list(await session.scalars(select(AiEditRecord).order_by(AiEditRecord.created_at)))


async def test_an_edit_with_the_users_key_shows_what_it_took(
    ai_edit_service: MakeAiEditService, session: AsyncSession
) -> None:
    model = FakeModelClient(reply(GOOD_REPLY, reasoning=50, cached=800, cost_usd=0.002))
    service = ai_edit_service(FakeModelSource(model))

    result = await confirmed(service, make_request())

    assert isinstance(result, AiEditApplied) and result.usage is not None
    usage = result.model_dump(mode="json", by_alias=True)["usage"]
    assert {k: v for k, v in usage.items() if k != "seconds"} == {
        "modelCalls": 1,
        "inputTokens": 1000,
        "outputTokens": 200,
        "totalTokens": 1200,
        "reasoningTokens": 50,
        "cachedTokens": 800,
        "costUsd": 0.002,
        "modelSeconds": 1.5,
    }
    assert usage["seconds"] >= 0
    [turn] = (await service.thread(USER, PROJECT_ID)).turns
    assert turn.usage == result.usage
    [record] = await records(session)
    assert (record.input_tokens, record.output_tokens, record.reasoning_tokens, record.cached_tokens) == (
        1000,
        200,
        50,
        800,
    )
    assert (record.cost_usd, record.model_ms) == (0.002, 1500)
    assert record.duration_ms is not None and record.duration_ms >= 0


async def test_an_edit_with_makables_model_records_but_doesnt_show_it(
    ai_edit_service: MakeAiEditService, session: AsyncSession
) -> None:
    service = ai_edit_service(FakeModelSource(FakeModelClient(reply(GOOD_REPLY))))

    result = await confirmed(service, make_request(model=None))

    assert isinstance(result, AiEditApplied) and result.usage is None
    [turn] = (await service.thread(USER, PROJECT_ID)).turns
    assert turn.usage is None
    [record] = await records(session)
    assert (record.input_tokens, record.output_tokens, record.model_ms) == (1000, 200, 1500)


async def test_a_question_doesnt_lose_what_the_run_took_before_it(
    ai_edit_service: MakeAiEditService, session: AsyncSession
) -> None:
    model = FakeModelClient(reply(ASK, 400, 50), reply(GOOD_REPLY, 1000, 200))
    paused = await confirmed(ai_edit_service(FakeModelSource(model)), make_request())
    assert isinstance(paused, AiEditNeedsInput)

    # A closed tab: another graph instance on the same saved thread.
    service = ai_edit_service(FakeModelSource(model))
    answer = AiEditAnswer(run_id=paused.run_id, answer="#16a34a", base_commit=BASE_COMMIT)
    result = await confirm(service, await service.answer(USER, PROJECT_ID, answer))

    assert isinstance(result, AiEditApplied) and result.usage is not None
    assert (result.usage.model_calls, result.usage.input_tokens, result.usage.output_tokens) == (2, 1400, 250)
    assert result.usage.model_seconds == 3.0
    [record] = await records(session)
    assert (record.input_tokens, record.output_tokens) == (1400, 250)


async def test_a_rejected_edit_records_what_its_tries_took(
    ai_edit_service: MakeAiEditService, session: AsyncSession
) -> None:
    bad = json.dumps({"summary": "x", "edits": [{"path": "src/App.tsx", "search": "nope", "replace": "y"}]})
    model = FakeModelClient(reply(bad), reply(bad))

    with pytest.raises(AiEditRejectedError):
        await confirmed(ai_edit_service(FakeModelSource(model)), make_request())

    [record] = await records(session)
    assert (record.outcome, record.input_tokens, record.output_tokens) == (AiEditOutcome.REJECTED, 2000, 400)


async def test_a_failed_edit_records_what_it_took_before_it_failed(
    ai_edit_service: MakeAiEditService, session: AsyncSession
) -> None:
    # The second call fails: the scripted replies run out.
    bad = json.dumps({"summary": "x", "edits": [{"path": "src/App.tsx", "search": "nope", "replace": "y"}]})
    model = FakeModelClient(reply(bad))

    with pytest.raises(IndexError):
        await confirmed(ai_edit_service(FakeModelSource(model)), make_request())

    [record] = await records(session)
    assert (record.outcome, record.input_tokens, record.model_ms) == (AiEditOutcome.FAILED, 1000, 1500)


async def test_a_creation_with_the_users_key_shows_what_it_took(ai_services: AiServices, session: AsyncSession) -> None:
    model = FakeModelClient(*(reply(text, 100, 10) for text in replies()))
    request = AiCreateRequest.model_validate({"projectId": str(NEW), "prompt": PROMPT, "model": OWN_MODEL})
    asked = result_of(await collect(await ai_services.create(FakeModelSource(model)).create(USER, request)))
    assert asked["status"] == "needs_input" and "usage" not in asked

    answer = AiCreateAnswer(run_id=asked["runId"], answer="Ada's Bakery")
    created = result_of(await collect(await ai_services.create(FakeModelSource(model)).answer(USER, NEW, answer)))

    # The questions about the site (before the pause), the plan and three files.
    usage = created["usage"]
    assert (usage["modelCalls"], usage["inputTokens"], usage["outputTokens"], usage["totalTokens"]) == (5, 500, 50, 550)
    [turn] = (await ai_services.edit(FakeModelSource(model)).thread(USER, NEW)).turns
    assert turn.usage is not None and turn.usage.input_tokens == 500
    [record] = await records(session)
    assert (record.input_tokens, record.output_tokens, record.model_ms) == (500, 50, 7500)


async def test_a_creation_with_makables_model_doesnt_show_it(ai_services: AiServices) -> None:
    model = FakeModelClient(*(reply(text) for text in replies()))
    request = AiCreateRequest(project_id=NEW, prompt=PROMPT)
    asked = result_of(await collect(await ai_services.create(FakeModelSource(model)).create(USER, request)))
    answer = AiCreateAnswer(run_id=asked["runId"], answer="Ada's Bakery")

    created = result_of(await collect(await ai_services.create(FakeModelSource(model)).answer(USER, NEW, answer)))

    assert created["status"] == "created" and "usage" not in created
    [turn] = (await ai_services.edit(FakeModelSource(model)).thread(USER, NEW)).turns
    assert turn.usage is None
