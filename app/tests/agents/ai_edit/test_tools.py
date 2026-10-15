import pytest

from app.agents.ai_edit.tools.answer import extract_json
from app.agents.ai_edit.tools.edits import check_edits
from app.agents.ai_edit.tools.rules import tier_2_rule
from app.schemas.ai_edit import FileEdit
from app.tests.ai_edit_fixtures import CONTENT_PATH, GOOD_EDITS, HERO_PATH, make_request
from app.utils.file_edits import FileEditError


def test_site_wide_requests_and_requests_with_no_files_go_to_tier_2() -> None:
    assert tier_2_rule(make_request()) is None
    assert "a page" in (tier_2_rule(make_request(instruction="Add a page for my blog")) or "")
    assert "No file" in (tier_2_rule(make_request(files=[])) or "")


def test_check_edits_dry_runs_the_edits() -> None:
    request = make_request()

    assert check_edits(request, [FileEdit.model_validate(e) for e in GOOD_EDITS]).changed == [HERO_PATH]
    assert check_edits(request, [FileEdit(path=CONTENT_PATH, search='"Engineer"', replace='"Builder"')]).changed


@pytest.mark.parametrize(
    ("edit", "error"),
    [
        (FileEdit(path=HERO_PATH, search="</a>", replace=""), "syntax error"),
        (FileEdit(path=CONTENT_PATH, search='"Engineer"', replace="'Engineer',"), "valid JSON"),
        (FileEdit(path="src/index.css", search="}", replace=""), "braces"),
        (FileEdit(path="package.json", search="a", replace="b"), "can't be edited"),
        (FileEdit(path=".github/workflows/deploy.yml", search="a", replace="b"), "can't be edited"),
        (FileEdit(path=HERO_PATH, search="bg-blue-600", replace="bg-blue-600"), "don't change any file"),
    ],
)
def test_check_edits_rejects_broken_results(edit: FileEdit, error: str) -> None:
    with pytest.raises(FileEditError, match=error):
        check_edits(make_request(), [edit])


def test_an_empty_edit_list_is_rejected() -> None:
    with pytest.raises(FileEditError, match="don't change any file"):
        check_edits(make_request(), [])


def test_extract_json_handles_fences_and_prose() -> None:
    assert extract_json('Sure!\n```json\n{"a":1}\n```') == {"a": 1}
    with pytest.raises(ValueError, match="did not return JSON"):
        extract_json("no json")
