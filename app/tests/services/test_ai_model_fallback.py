"""When the user's model stops answering, an edit or a creation goes on with another of their models and says so."""

from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.model import AiKeyInvalidError, ModelRateLimitedError, ModelUnavailableError
from app.constants.ai_edit import AiEditOutcome
from app.models.site import AiEditRecord
from app.schemas.ai_create import AiCreateRequest
from app.schemas.ai_edit import AiEditApplied, AiEditAwaitingResult, AiModelSwitch
from app.tests.ai_edit_fixtures import GOOD_REPLY, USER, MakeAiEditService, confirm, make_request
from app.tests.conftest import AiServices
from app.tests.fakes import FakeModelClient, FakeModelSource
from app.tests.services.test_site_create import NEW, PROMPT, collect, replies, result_of

PICKED_KEY = UUID("11111111-2222-4333-8444-555555555555")
PICK = {"keyId": str(PICKED_KEY), "model": "gemini-antigravity"}


async def records(session: AsyncSession) -> list[AiEditRecord]:
    session.expire_all()
    return list(await session.scalars(select(AiEditRecord).order_by(AiEditRecord.created_at)))


async def test_an_edit_goes_on_with_the_next_model_and_says_so(
    ai_edit_service: MakeAiEditService, session: AsyncSession
) -> None:
    picked = FakeModelClient(ModelUnavailableError(), name="gemini-antigravity")
    service = ai_edit_service(FakeModelSource(picked, FakeModelClient(GOOD_REPLY, name="gemini-2.5-flash")))

    waiting = await service.edit(USER, make_request(model=PICK))

    assert isinstance(waiting, AiEditAwaitingResult)
    assert waiting.model_switches == [
        AiModelSwitch(
            failed="gemini-antigravity",
            reason="ai_unavailable",
            model="gemini-2.5-flash",
            message="gemini-antigravity isn't answering, so gemini-2.5-flash is taking over.",
        )
    ]
    applied = await confirm(service, waiting)
    assert isinstance(applied, AiEditApplied)
    [record] = await records(session)
    assert (record.outcome, record.model) == (AiEditOutcome.APPLIED, "gemini-2.5-flash")


async def test_an_edit_that_didnt_switch_says_nothing(ai_edit_service: MakeAiEditService) -> None:
    service = ai_edit_service(FakeModelSource(FakeModelClient(GOOD_REPLY), FakeModelClient()))

    waiting = await service.edit(USER, make_request(model=PICK))

    assert waiting.model_switches is None
    assert "modelSwitches" not in waiting.model_dump(by_alias=True, exclude_none=True)


async def test_a_key_refused_on_the_way_is_marked_even_when_the_edit_works(ai_edit_service: MakeAiEditService) -> None:
    source = FakeModelSource(FakeModelClient(AiKeyInvalidError()), FakeModelClient(GOOD_REPLY))

    await ai_edit_service(source).edit(USER, make_request(model=PICK))

    assert source.refused == [PICKED_KEY]


async def test_when_every_model_fails_the_picks_error_is_raised_and_recorded(
    ai_edit_service: MakeAiEditService, session: AsyncSession
) -> None:
    picked = ModelRateLimitedError(30)
    source = FakeModelSource(FakeModelClient(picked), FakeModelClient(ModelUnavailableError()))

    with pytest.raises(ModelRateLimitedError) as err:
        await ai_edit_service(source).edit(USER, make_request(model=PICK))

    assert err.value is picked
    [record] = await records(session)
    assert (record.outcome, record.summary) == (AiEditOutcome.FAILED, "ai_rate_limited")


async def test_a_creation_tells_the_browser_when_it_switches(ai_services: AiServices) -> None:
    picked = FakeModelClient(ModelRateLimitedError(30), name="gpt-5")
    source = FakeModelSource(picked, FakeModelClient(*replies(), name="o4-mini"))
    request = AiCreateRequest.model_validate({"projectId": str(NEW), "prompt": PROMPT, "model": PICK})

    events = await collect(await ai_services.create(source).create(USER, request))

    message = "gpt-5 is rate limiting your key, so o4-mini is taking over."
    assert {"type": "progress", "stage": "model", "message": message} in events
    asked = result_of(events)
    assert asked["status"] == "needs_input"
    assert asked["modelSwitches"] == [
        {"failed": "gpt-5", "reason": "ai_rate_limited", "model": "o4-mini", "message": message}
    ]
