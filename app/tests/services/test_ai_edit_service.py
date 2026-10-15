"""An AI edit end to end, on the fake GitHub: the agent reads the site at the base commit, checks the model's edits and
commits them."""

import json

import pytest

from app.agents.ai_edit.prompts.system import REPAIR_PROMPT, SYSTEM_PROMPT
from app.schemas.ai_edit import AiEditApplied, AiEditEscalated
from app.schemas.chat import ImagePart
from app.services.ai_edit_service import AiEditRejectedError
from app.services.site_service import SiteConflictError, SiteNotFoundError
from app.tests.ai_edit_fixtures import (
    BASE_COMMIT,
    GOOD_REPLY,
    GREEN_HERO,
    HERO_PATH,
    REPO,
    USER,
    MakeAiEditService,
    make_request,
)
from app.tests.fakes import FakeGithubClient, FakeModelClient, FakeModelSource

# A 1x1 PNG.
PNG = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


async def test_checked_edits_are_committed_and_the_changed_files_come_back(
    ai_edit_service: MakeAiEditService, github: FakeGithubClient
) -> None:
    model = FakeModelClient(f"```json\n{GOOD_REPLY}\n```")

    result = await ai_edit_service(FakeModelSource(model)).edit(USER, make_request())

    assert isinstance(result, AiEditApplied)
    assert (result.summary, result.files) == ("Made it green.", {HERO_PATH: GREEN_HERO})
    assert result.commit_sha == github.heads[REPO].commit_sha != BASE_COMMIT
    assert github.files[REPO][HERO_PATH].content == GREEN_HERO
    assert github.commits[-1] == (REPO, "Make the contact button green", [HERO_PATH])
    assert result.debug is None
    [messages] = model.calls
    assert [m.role for m in messages] == ["user"]
    assert messages[0].text.startswith(SYSTEM_PROMPT)
    assert '   5|       <a className="bg-blue-600">Contact</a>' in messages[0].text


async def test_a_rejected_answer_is_retried_once_with_the_error(ai_edit_service: MakeAiEditService) -> None:
    bad = json.dumps({"summary": "x", "edits": [{"path": HERO_PATH, "search": "nope", "replace": "y"}]})
    model = FakeModelClient(bad, GOOD_REPLY)

    result = await ai_edit_service(FakeModelSource(model), debug=True).edit(USER, make_request())

    assert isinstance(result, AiEditApplied)
    assert result.debug is not None
    assert (result.debug.attempts, result.debug.model) == (2, "fake-model")
    retry = model.calls[1]
    assert [m.role for m in retry] == ["user", "assistant", "user"]
    assert retry[1].content == bad
    assert retry[2].text.startswith(REPAIR_PROMPT)
    assert f"<rejection_report>\nEdit 1 of 1 ({HERO_PATH}) was rejected: the search text was not found" in retry[2].text


async def test_the_model_can_ask_for_more_code_before_answering(ai_edit_service: MakeAiEditService) -> None:
    ask = '{"needContext": {"path": "src/App.tsx", "lines": [3, 9]}}'
    model = FakeModelClient(ask, GOOD_REPLY)

    result = await ai_edit_service(FakeModelSource(model)).edit(USER, make_request())

    assert isinstance(result, AiEditApplied)
    second = model.calls[1]
    assert [m.role for m in second] == ["user", "assistant", "user"]
    assert second[1].content == ask
    assert second[2].text.startswith("Here is src/App.tsx, lines 3-9. Now answer with edits, or escalate.")
    assert '<file path="src/App.tsx" lines="3-9" total_lines="9">\n   3| export function App() {' in second[2].text


async def test_asking_for_code_too_often_counts_as_a_rejected_answer(ai_edit_service: MakeAiEditService) -> None:
    ask = '{"needContext": {"path": "src/App.tsx"}}'
    model = FakeModelClient(ask, ask, ask, ask)

    with pytest.raises(AiEditRejectedError):
        await ai_edit_service(FakeModelSource(model)).edit(USER, make_request())

    # Two rounds of code, then the third ask is rejected and repaired, and the repair asks again.
    assert len(model.calls) == 4
    assert "you can't ask for more code now" in model.calls[3][-1].text


async def test_a_new_file_can_be_created(ai_edit_service: MakeAiEditService, github: FakeGithubClient) -> None:
    footer = "export function Footer() {\n  return <footer>Bye</footer>\n}\n"
    reply = json.dumps(
        {
            "summary": "Added a footer.",
            "edits": [
                {"path": "src/components/Footer.tsx", "search": "", "replace": footer},
                {"path": "src/App.tsx", "search": "      <Hero />", "replace": "      <Hero />\n      <Footer />"},
            ],
        }
    )

    result = await ai_edit_service(FakeModelSource(FakeModelClient(reply))).edit(USER, make_request())

    assert isinstance(result, AiEditApplied)
    assert result.files["src/components/Footer.tsx"] == footer
    assert github.files[REPO]["src/components/Footer.tsx"].content == footer


async def test_the_picture_goes_only_to_models_that_read_images(
    ai_edit_service: MakeAiEditService, github: FakeGithubClient
) -> None:
    sees = FakeModelClient(GOOD_REPLY, accepts_images=True)
    red = {"path": HERO_PATH, "search": "bg-emerald-600", "replace": "bg-red-600"}
    blind = FakeModelClient(json.dumps({"summary": "Made it red.", "edits": [red]}))

    await ai_edit_service(FakeModelSource(sees)).edit(USER, make_request(screenshot=PNG))

    content = sees.calls[0][0].content
    assert isinstance(content, list)
    assert isinstance(content[1], ImagePart) and content[1].image_url.url == PNG
    head = github.heads[REPO].commit_sha
    await ai_edit_service(FakeModelSource(blind)).edit(
        USER, make_request(screenshot=PNG, baseCommit=head, instruction="Make it red")
    )
    assert isinstance(blind.calls[0][0].content, str)


async def test_the_model_can_escalate_to_tier_2(ai_edit_service: MakeAiEditService, github: FakeGithubClient) -> None:
    model = FakeModelClient('{"escalate":"Needs the navbar too."}')

    result = await ai_edit_service(FakeModelSource(model)).edit(USER, make_request())

    assert isinstance(result, AiEditEscalated)
    assert result.reason == "Needs the navbar too."
    assert github.commits == []


async def test_rule_based_tier_2_skips_the_model(ai_edit_service: MakeAiEditService) -> None:
    model = FakeModelClient()

    result = await ai_edit_service(FakeModelSource(model)).edit(USER, make_request(instruction="Install framer motion"))

    assert isinstance(result, AiEditEscalated)
    assert model.calls == []


async def test_two_bad_answers_are_an_error(ai_edit_service: MakeAiEditService, github: FakeGithubClient) -> None:
    model = FakeModelClient("not json", '{"summary": "x"}')

    with pytest.raises(AiEditRejectedError):
        await ai_edit_service(FakeModelSource(model)).edit(USER, make_request())
    assert len(model.calls) == 2
    assert "did not return JSON" in model.calls[1][-1].text
    assert github.commits == []


async def test_a_site_changed_elsewhere_is_a_conflict_before_the_model_is_asked(
    ai_edit_service: MakeAiEditService, github: FakeGithubClient
) -> None:
    github.edit_on_github(REPO, "src/index.css", "body {}\n")
    head = github.heads[REPO].commit_sha

    model = FakeModelClient(GOOD_REPLY)
    with pytest.raises(SiteConflictError) as caught:
        await ai_edit_service(FakeModelSource(model)).edit(USER, make_request())

    assert caught.value.details == {"commitSha": head}
    assert model.calls == []
    assert github.heads[REPO].commit_sha == head
    assert github.files[REPO][HERO_PATH].content != GREEN_HERO


async def test_an_unknown_site_is_not_found(ai_edit_service: MakeAiEditService) -> None:
    with pytest.raises(SiteNotFoundError):
        await ai_edit_service(FakeModelSource(FakeModelClient())).edit(
            USER, make_request(projectId="11111111-1111-4111-8111-111111111111")
        )


async def test_a_line_number_copied_into_a_search_still_works(ai_edit_service: MakeAiEditService) -> None:
    reply = json.dumps(
        {
            "summary": "Made it green.",
            "edits": [
                {
                    "path": HERO_PATH,
                    "search": '   5|       <a className="bg-blue-600">Contact</a>',
                    "replace": '   5|       <a className="bg-emerald-600">Contact</a>',
                }
            ],
        }
    )

    result = await ai_edit_service(FakeModelSource(FakeModelClient(reply))).edit(USER, make_request())

    assert isinstance(result, AiEditApplied)
    assert result.files == {HERO_PATH: GREEN_HERO}


async def test_a_site_changed_during_the_edit_is_a_conflict_and_nothing_is_written(
    ai_edit_service: MakeAiEditService, github: FakeGithubClient
) -> None:
    github.races = 1  # Another tab saves just before the commit lands.

    with pytest.raises(SiteConflictError):
        await ai_edit_service(FakeModelSource(FakeModelClient(GOOD_REPLY))).edit(USER, make_request())

    assert github.files[REPO][HERO_PATH].content != GREEN_HERO
