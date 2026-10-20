"""Questions for the user: a run pauses on one, survives a closed tab (a new graph instance on the same saved thread),
and continues with the answer on the site as it is then."""

import asyncio
import json

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.model import ModelReply, ModelUnavailableError
from app.constants.ai_edit import AiEditOutcome
from app.models.site import AiEditRecord
from app.schemas.ai_edit import (
    AiEditAnswer,
    AiEditApplied,
    AiEditAwaitingResult,
    AiEditNeedsInput,
    AiEditPreviewReport,
)
from app.schemas.chat import ChatMessage
from app.services.ai_edit_service import AiRunSupersededError
from app.services.site_service import SiteNotFoundError
from app.tests.ai_edit_fixtures import (
    BASE_COMMIT,
    GOOD_REPLY,
    HERO_PATH,
    PROJECT_ID,
    REPO,
    USER,
    MakeAiEditService,
    confirm,
    confirmed,
    make_request,
)
from app.tests.fakes import FakeGithubClient, FakeModelClient, FakeModelSource
from app.tests.services.test_ai_edit_threads import OTHER_USER, BlockingModel

ASK = json.dumps({"ask": {"question": "Which colour?", "options": ["#16a34a", "#dc2626"], "allowFreeText": True}})


async def records(session: AsyncSession) -> list[AiEditRecord]:
    session.expire_all()
    return list(await session.scalars(select(AiEditRecord).order_by(AiEditRecord.created_at)))


def answer(paused: AiEditNeedsInput, text: str = "#16a34a", base: str = BASE_COMMIT) -> AiEditAnswer:
    return AiEditAnswer(run_id=paused.run_id, answer=text, base_commit=base)


async def ask_first(ai_edit_service: MakeAiEditService, *replies: str) -> tuple[AiEditNeedsInput, FakeModelClient]:
    model = FakeModelClient(ASK, *replies)
    paused = await confirmed(ai_edit_service(FakeModelSource(model)), make_request(instruction="Change the colour"))
    assert isinstance(paused, AiEditNeedsInput)
    return paused, model


async def test_a_question_pauses_the_run_and_survives_a_closed_tab(
    ai_edit_service: MakeAiEditService, session: AsyncSession, github: FakeGithubClient
) -> None:
    paused, model = await ask_first(ai_edit_service, GOOD_REPLY)

    assert paused.question.question == "Which colour?"
    assert paused.question.options == ["#16a34a", "#dc2626"]
    assert await records(session) == []
    assert github.commits == []

    # A new graph instance on the same saved thread: a closed tab, or a restarted server.
    thread = await ai_edit_service(FakeModelSource(model)).thread(USER, PROJECT_ID)
    assert thread.status == "needs_input"
    assert thread.run is not None
    assert (thread.run.run_id, thread.run.instruction, thread.run.question) == (
        paused.run_id,
        "Change the colour",
        paused.question,
    )

    result = await confirm(
        ai_edit_service(FakeModelSource(model)),
        await ai_edit_service(FakeModelSource(model)).answer(USER, PROJECT_ID, answer(paused)),
    )

    assert isinstance(result, AiEditApplied)
    assert result.run_id == paused.run_id
    after = model.calls[1]
    assert [m.role for m in after] == ["user", "assistant", "user"]
    assert after[1].content == ASK
    assert "<answer>\n#16a34a\n</answer>" in after[2].text
    thread = await ai_edit_service(FakeModelSource(model)).thread(USER, PROJECT_ID)
    assert (thread.status, thread.run) == ("idle", None)
    [turn] = thread.turns
    assert [(q.question, q.answer) for q in turn.questions] == [("Which colour?", "#16a34a")]
    assert turn.commit_sha == result.commit_sha
    [record] = await records(session)
    assert (record.outcome, record.attempts) == (AiEditOutcome.APPLIED, 2)


async def test_a_commit_finished_after_stop_keeps_the_answer_in_the_chat(
    ai_edit_service: MakeAiEditService, github: FakeGithubClient
) -> None:
    paused, model = await ask_first(ai_edit_service, GOOD_REPLY)
    service = ai_edit_service(FakeModelSource(model))
    waiting = await service.answer(USER, PROJECT_ID, answer(paused))
    assert isinstance(waiting, AiEditAwaitingResult)
    github.commit_delay = 0.2
    run = asyncio.create_task(service.report(USER, PROJECT_ID, AiEditPreviewReport(run_id=waiting.run_id, ok=True)))
    await github.commit_started.wait()
    await asyncio.sleep(0.05)  # Into the commit.

    run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run

    [turn] = (await service.thread(USER, PROJECT_ID)).turns
    assert turn.outcome == AiEditOutcome.APPLIED
    assert [(q.question, q.answer) for q in turn.questions] == [("Which colour?", "#16a34a")]


async def test_the_answer_works_on_the_site_as_it_is_then(
    ai_edit_service: MakeAiEditService, github: FakeGithubClient
) -> None:
    paused, model = await ask_first(ai_edit_service, GOOD_REPLY)
    github.edit_on_github(REPO, HERO_PATH, github.files[REPO][HERO_PATH].content.replace("Contact", "Say hi"))
    head = github.heads[REPO].commit_sha

    result = await confirm(
        ai_edit_service(FakeModelSource(model)),
        await ai_edit_service(FakeModelSource(model)).answer(USER, PROJECT_ID, answer(paused, base=head)),
    )

    assert isinstance(result, AiEditApplied)
    assert ">Say hi</a>" in model.calls[1][0].text
    assert ">Say hi</a>" in github.files[REPO][HERO_PATH].content
    assert "bg-emerald-600" in github.files[REPO][HERO_PATH].content


async def test_a_new_instruction_while_paused_skips_the_paused_run(
    ai_edit_service: MakeAiEditService, session: AsyncSession
) -> None:
    paused, _ = await ask_first(ai_edit_service)
    model = FakeModelClient(GOOD_REPLY)

    result = await confirmed(ai_edit_service(FakeModelSource(model)), make_request())

    assert isinstance(result, AiEditApplied)
    skipped, applied = await records(session)
    assert (skipped.instruction, skipped.outcome) == ("Change the colour", AiEditOutcome.SKIPPED)
    assert applied.outcome == AiEditOutcome.APPLIED
    with pytest.raises(AiRunSupersededError):
        await confirm(
            ai_edit_service(FakeModelSource(model)),
            await ai_edit_service(FakeModelSource(model)).answer(
                USER, PROJECT_ID, answer(paused, base=result.commit_sha)
            ),
        )


async def test_dismiss_ends_the_paused_run(ai_edit_service: MakeAiEditService, session: AsyncSession) -> None:
    paused, model = await ask_first(ai_edit_service)
    service = ai_edit_service(FakeModelSource(model))

    await service.dismiss(USER, PROJECT_ID)
    await service.dismiss(USER, PROJECT_ID)  # Nothing waiting is fine.

    assert (await service.thread(USER, PROJECT_ID)).status == "idle"
    [record] = await records(session)
    assert record.outcome == AiEditOutcome.SKIPPED
    with pytest.raises(AiRunSupersededError):
        await service.answer(USER, PROJECT_ID, answer(paused))


async def test_another_user_cant_see_or_answer_the_question(ai_edit_service: MakeAiEditService) -> None:
    paused, model = await ask_first(ai_edit_service)
    service = ai_edit_service(FakeModelSource(model))

    with pytest.raises(SiteNotFoundError):
        await service.thread(OTHER_USER, PROJECT_ID)
    with pytest.raises(SiteNotFoundError):
        await service.answer(OTHER_USER, PROJECT_ID, answer(paused))
    with pytest.raises(SiteNotFoundError):
        await service.dismiss(OTHER_USER, PROJECT_ID)


async def test_start_over_removes_the_paused_run(ai_edit_service: MakeAiEditService) -> None:
    _, model = await ask_first(ai_edit_service)
    service = ai_edit_service(FakeModelSource(model))

    await service.start_over(USER, PROJECT_ID)

    thread = await service.thread(USER, PROJECT_ID)
    assert (thread.status, thread.turns) == ("idle", [])


async def test_only_two_questions_per_request(ai_edit_service: MakeAiEditService) -> None:
    paused, model = await ask_first(ai_edit_service, ASK, ASK, GOOD_REPLY)
    service = ai_edit_service(FakeModelSource(model))

    second = await service.answer(USER, PROJECT_ID, answer(paused))
    assert isinstance(second, AiEditNeedsInput)
    result = await confirm(service, await service.answer(USER, PROJECT_ID, answer(second, "green")))

    assert isinstance(result, AiEditApplied)
    # The third question was rejected, and the repair answered with edits.
    assert "you can't ask the user anything more" in model.calls[3][-1].text


async def test_stop_after_answering_ends_the_run(ai_edit_service: MakeAiEditService, session: AsyncSession) -> None:
    paused, _ = await ask_first(ai_edit_service)
    blocking = BlockingModel()
    run = asyncio.create_task(ai_edit_service(FakeModelSource(blocking)).answer(USER, PROJECT_ID, answer(paused)))
    await blocking.called.wait()

    run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run

    thread = await ai_edit_service(FakeModelSource(blocking)).thread(USER, PROJECT_ID)
    assert thread.status == "idle"
    [record] = await records(session)
    assert record.outcome == AiEditOutcome.CANCELLED


async def test_a_failure_after_answering_ends_the_run(
    ai_edit_service: MakeAiEditService, session: AsyncSession
) -> None:
    paused, _ = await ask_first(ai_edit_service)

    class Failing:
        name = "failing"
        accepts_images = False

        async def complete(self, messages: list[ChatMessage]) -> ModelReply:
            raise ModelUnavailableError()

    with pytest.raises(ModelUnavailableError):
        await confirm(
            ai_edit_service(FakeModelSource(Failing())),
            await ai_edit_service(FakeModelSource(Failing())).answer(USER, PROJECT_ID, answer(paused)),
        )

    assert (await ai_edit_service(FakeModelSource(Failing())).thread(USER, PROJECT_ID)).status == "idle"
    [record] = await records(session)
    assert (record.outcome, record.summary) == (AiEditOutcome.FAILED, "ai_unavailable")
    with pytest.raises(AiRunSupersededError):
        await confirm(
            ai_edit_service(FakeModelSource(FakeModelClient())),
            await ai_edit_service(FakeModelSource(FakeModelClient())).answer(USER, PROJECT_ID, answer(paused)),
        )
