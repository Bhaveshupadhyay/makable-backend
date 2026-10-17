"""The preview check: the AI's edits are tried in the SPA's preview before anything is committed. A failure goes back to
the model with what the preview showed; after two, the request gives up."""

import json

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants.ai_edit import AiEditOutcome
from app.models.site import AiEditRecord
from app.schemas.ai_edit import AiEditApplied, AiEditAwaitingResult, AiEditPreviewReport, PreviewError
from app.services.ai_edit_service import AiEditBrokePreviewError, AiRunSupersededError
from app.services.site_service import VERIFIED_SITES
from app.tests.ai_edit_fixtures import (
    GOOD_REPLY,
    GREEN_HERO,
    HERO,
    HERO_PATH,
    PROJECT_ID,
    REPO,
    USER,
    MakeAiEditService,
    make_request,
)
from app.tests.fakes import FakeGithubClient, FakeModelClient, FakeModelSource

BROKEN = json.dumps(
    {
        "summary": "Added an icon.",
        "edits": [{"path": HERO_PATH, "search": "      <h1>Ada</h1>", "replace": "      <Star />\n      <h1>Ada</h1>"}],
    }
)
ERROR = PreviewError(message="ReferenceError: Star is not defined", path=HERO_PATH, line=4)


async def records(session: AsyncSession) -> list[AiEditRecord]:
    session.expire_all()
    return list(await session.scalars(select(AiEditRecord)))


def ok(waiting: AiEditAwaitingResult) -> AiEditPreviewReport:
    return AiEditPreviewReport(run_id=waiting.run_id, ok=True)


def failed(waiting: AiEditAwaitingResult) -> AiEditPreviewReport:
    return AiEditPreviewReport(run_id=waiting.run_id, ok=False, error=ERROR)


async def test_edits_wait_for_the_preview_before_anything_is_committed(
    ai_edit_service: MakeAiEditService, github: FakeGithubClient, session: AsyncSession
) -> None:
    model = FakeModelClient(GOOD_REPLY)

    waiting = await ai_edit_service(FakeModelSource(model)).edit(USER, make_request())

    assert isinstance(waiting, AiEditAwaitingResult)
    assert (waiting.summary, waiting.files) == ("Made it green.", {HERO_PATH: GREEN_HERO})
    assert github.commits == []
    assert await records(session) == []
    # A closed tab: the edits to try come back with the thread.
    thread = await ai_edit_service(FakeModelSource(model)).thread(USER, PROJECT_ID)
    assert thread.status == "awaiting_result"
    assert thread.run is not None and thread.run.preview is not None
    assert (thread.run.run_id, thread.run.preview.files) == (waiting.run_id, {HERO_PATH: GREEN_HERO})

    result = await ai_edit_service(FakeModelSource(model)).report(USER, PROJECT_ID, ok(waiting))

    assert isinstance(result, AiEditApplied)
    assert github.files[REPO][HERO_PATH].content == GREEN_HERO
    assert (await ai_edit_service(FakeModelSource(model)).thread(USER, PROJECT_ID)).status == "idle"


async def test_the_chat_loads_without_the_edits_to_try_when_the_repo_cant_be_read(
    ai_edit_service: MakeAiEditService, github: FakeGithubClient
) -> None:
    service = ai_edit_service(FakeModelSource(FakeModelClient(GOOD_REPLY)))
    waiting = await service.edit(USER, make_request())
    assert isinstance(waiting, AiEditAwaitingResult)
    VERIFIED_SITES.clear()
    del github.repos[REPO]  # Deleted or renamed on GitHub.

    thread = await service.thread(USER, PROJECT_ID)

    assert thread.status == "awaiting_result"
    assert thread.run is not None
    assert (thread.run.run_id, thread.run.preview) == (waiting.run_id, None)


async def test_a_preview_error_goes_back_to_the_model(
    ai_edit_service: MakeAiEditService, github: FakeGithubClient
) -> None:
    model = FakeModelClient(BROKEN, GOOD_REPLY)
    service = ai_edit_service(FakeModelSource(model))

    first = await service.edit(USER, make_request())
    assert isinstance(first, AiEditAwaitingResult)
    second = await service.report(USER, PROJECT_ID, failed(first))

    assert isinstance(second, AiEditAwaitingResult)
    assert second.run_id == first.run_id
    assert second.files == {HERO_PATH: GREEN_HERO}
    report = model.calls[1][-1].text
    assert "The preview showed this error: ⟦ReferenceError: Star is not defined⟧" in report
    assert f"It's in {HERO_PATH}, line 4." in report
    assert "   4>       <Star />" in report
    assert github.commits == []
    assert isinstance(await service.report(USER, PROJECT_ID, ok(second)), AiEditApplied)


async def test_the_request_gives_up_when_the_preview_keeps_failing(
    ai_edit_service: MakeAiEditService, github: FakeGithubClient, session: AsyncSession
) -> None:
    service = ai_edit_service(FakeModelSource(FakeModelClient(BROKEN, BROKEN, BROKEN)))
    waiting = await service.edit(USER, make_request())
    for _ in range(2):
        assert isinstance(waiting, AiEditAwaitingResult)
        waiting = await service.report(USER, PROJECT_ID, failed(waiting))
    assert isinstance(waiting, AiEditAwaitingResult)

    with pytest.raises(AiEditBrokePreviewError):
        await service.report(USER, PROJECT_ID, failed(waiting))

    assert github.commits == []
    assert github.files[REPO][HERO_PATH].content == HERO
    [record] = await records(session)
    assert record.outcome == AiEditOutcome.REJECTED
    assert record.summary is not None and record.summary.startswith("The preview failed: ReferenceError")
    assert (await service.thread(USER, PROJECT_ID)).turns == []


async def test_a_report_for_another_run_is_superseded(ai_edit_service: MakeAiEditService) -> None:
    service = ai_edit_service(FakeModelSource(FakeModelClient(GOOD_REPLY, GOOD_REPLY)))
    first = await service.edit(USER, make_request())
    assert isinstance(first, AiEditAwaitingResult)

    # A new instruction replaces the run that waits for the preview.
    second = await service.edit(USER, make_request(instruction="Green, please"))

    assert isinstance(second, AiEditAwaitingResult)
    with pytest.raises(AiRunSupersededError):
        await service.report(USER, PROJECT_ID, ok(first))
    assert isinstance(await service.report(USER, PROJECT_ID, ok(second)), AiEditApplied)
    with pytest.raises(AiRunSupersededError):
        await service.report(USER, PROJECT_ID, ok(second))
