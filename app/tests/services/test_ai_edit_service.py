"""Mirrors the SPA's old dev endpoint tests (`apps/web/dev-server/ai-edit.test.ts`)."""

import json
from uuid import uuid4

import pytest

from app.agents.ai_edit.prompts.system import REPAIR_PROMPT, SYSTEM_PROMPT
from app.schemas.ai_edit import AiEditTier1, AiEditTier2, FileEdit
from app.services.ai_edit_service import AiEditRejectedError
from app.tests.ai_edit_fixtures import (
    GOOD_EDITS,
    GOOD_REPLY,
    HERO_PATH,
    MakeAiEditService,
    make_request,
)
from app.tests.fakes import FakeModelClient, FakeModelSource

USER_ID = uuid4()


async def test_returns_checked_tier_1_edits(ai_edit_service: MakeAiEditService) -> None:
    model = FakeModelClient(f"```json\n{GOOD_REPLY}\n```")

    result = await ai_edit_service(FakeModelSource(model)).edit(USER_ID, make_request())

    assert result == AiEditTier1(summary="Made it green.", edits=[FileEdit.model_validate(e) for e in GOOD_EDITS])
    assert result.debug is None
    [messages] = model.calls
    assert [m.role for m in messages] == ["user"]
    assert messages[0].content.startswith(SYSTEM_PROMPT)


async def test_a_rejected_answer_is_retried_once_with_the_error(ai_edit_service: MakeAiEditService) -> None:
    bad = json.dumps({"summary": "x", "edits": [{"path": HERO_PATH, "search": "nope", "replace": "y"}]})
    model = FakeModelClient(bad, GOOD_REPLY)

    result = await ai_edit_service(FakeModelSource(model), debug=True).edit(USER_ID, make_request())

    assert isinstance(result, AiEditTier1)
    assert result.debug is not None
    assert (result.debug.attempts, result.debug.model) == (2, "fake-model")
    retry = model.calls[1]
    assert [m.role for m in retry] == ["user", "assistant", "user"]
    assert retry[1].content == bad
    assert retry[2].content.startswith(REPAIR_PROMPT)
    assert f"<rejection_report>\nEdit 1 of 1 ({HERO_PATH}) was rejected: the search text was not found" in (
        retry[2].content
    )


async def test_the_model_can_escalate_to_tier_2(ai_edit_service: MakeAiEditService) -> None:
    model = FakeModelClient('{"escalate":"Needs the navbar too."}')

    assert await ai_edit_service(FakeModelSource(model)).edit(USER_ID, make_request()) == AiEditTier2(
        reason="Needs the navbar too."
    )


async def test_rule_based_tier_2_skips_the_model(ai_edit_service: MakeAiEditService) -> None:
    model = FakeModelClient()

    result = await ai_edit_service(FakeModelSource(model)).edit(
        USER_ID, make_request(instruction="Install framer motion")
    )

    assert isinstance(result, AiEditTier2)
    assert model.calls == []


async def test_two_bad_answers_are_an_error(ai_edit_service: MakeAiEditService) -> None:
    model = FakeModelClient("not json", '{"summary": "x"}')

    with pytest.raises(AiEditRejectedError):
        await ai_edit_service(FakeModelSource(model)).edit(USER_ID, make_request())
    assert len(model.calls) == 2
    assert "did not return JSON" in model.calls[1][-1].content


async def test_a_forbidden_edit_after_a_failing_one_still_gets_its_retry(ai_edit_service: MakeAiEditService) -> None:
    bad = json.dumps(
        {
            "summary": "x",
            "edits": [
                {"path": HERO_PATH, "search": "nope", "replace": "x"},
                {"path": "package.json", "search": "a", "replace": "b"},
            ],
        }
    )
    model = FakeModelClient(bad, GOOD_REPLY)

    result = await ai_edit_service(FakeModelSource(model)).edit(uuid4(), make_request())

    assert isinstance(result, AiEditTier1)
    assert "Edit 2 of 2 (package.json) was rejected" in model.calls[1][-1].content
