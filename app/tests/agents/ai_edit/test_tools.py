import pytest

from app.agents.ai_edit.state import ContextRef, Selection
from app.agents.ai_edit.tools.answer import extract_json, parse_answer, without_line_numbers
from app.agents.ai_edit.tools.context import first_context, locate_selection, outline, requested_context
from app.agents.ai_edit.tools.edits import check_edits
from app.agents.ai_edit.tools.rules import tier_2_rule
from app.constants.ai_edit import CONTEXT_WINDOW_LINES, MAX_EXTRA_CONTEXT_LINES
from app.schemas.ai_edit import ContextRequest, FileEdit, ModelNeedsContext
from app.tests.ai_edit_fixtures import GOOD_EDITS, HERO, HERO_PATH, SITE_FILES, make_request
from app.utils.file_edits import FileEditError


def test_site_wide_requests_go_to_tier_2() -> None:
    assert tier_2_rule(make_request()) is None
    assert "a page" in (tier_2_rule(make_request(instruction="Add a page for my blog")) or "")


def test_check_edits_dry_runs_the_edits_on_the_sites_files() -> None:
    assert check_edits(SITE_FILES, [FileEdit.model_validate(e) for e in GOOD_EDITS]).changed == [HERO_PATH]
    created = FileEdit(path="src/components/Footer.tsx", search="", replace="export const Footer = () => <footer />\n")
    assert check_edits(SITE_FILES, [created]).changed == ["src/components/Footer.tsx"]


@pytest.mark.parametrize(
    ("edit", "error"),
    [
        (FileEdit(path=HERO_PATH, search="</a>", replace=""), "syntax error"),
        (FileEdit(path="src/index.css", search="}", replace=""), "braces"),
        (FileEdit(path="package.json", search="a", replace="b"), "can't be edited"),
        (FileEdit(path=".github/workflows/deploy.yml", search="a", replace="b"), "can't be edited"),
        (FileEdit(path=".makable/manifest.json", search="a", replace="b"), "can't be edited"),
        (FileEdit(path=HERO_PATH, search="bg-blue-600", replace="bg-blue-600"), "don't change any file"),
        (FileEdit(path="src/Missing.tsx", search="x", replace="y"), "no such file"),
    ],
)
def test_check_edits_rejects_broken_results(edit: FileEdit, error: str) -> None:
    with pytest.raises(FileEditError, match=error):
        check_edits(SITE_FILES, [edit])


def test_an_empty_edit_list_is_rejected() -> None:
    with pytest.raises(FileEditError, match="don't change any file"):
        check_edits(SITE_FILES, [])


def test_extract_json_handles_fences_and_prose() -> None:
    assert extract_json('Sure!\n```json\n{"a":1}\n```') == {"a": 1}
    with pytest.raises(ValueError, match="did not return JSON"):
        extract_json("no json")


def test_a_request_for_more_code_is_an_answer() -> None:
    answer = parse_answer('{"needContext": {"path": "src/App.tsx", "lines": [1, 20]}}')

    assert isinstance(answer, ModelNeedsContext)
    assert (answer.need_context.path, answer.need_context.lines) == ("src/App.tsx", [1, 20])


def test_line_numbers_copied_from_the_prompt_are_taken_out_of_edits() -> None:
    copied = FileEdit(
        path=HERO_PATH, search='   5|       <a className="bg-blue-600">', replace='   5|       <a className="x">'
    )
    kept = FileEdit(path=HERO_PATH, search="a: 1| b", replace="c")

    fixed, untouched = without_line_numbers([copied, kept])

    assert (fixed.search, fixed.replace) == ('      <a className="bg-blue-600">', '      <a className="x">')
    assert untouched == kept


def test_the_selection_is_the_preview_line_or_found_again_near_it() -> None:
    target = make_request().target
    assert target is not None

    assert locate_selection(SITE_FILES, target) == Selection(path=HERO_PATH, line=5, tag="a", text="Contact")
    moved = target.model_copy(update={"source": target.source.model_copy(update={"line": 2})})  # type: ignore[union-attr]
    assert locate_selection(SITE_FILES, moved)["line"] == 5  # type: ignore[index]
    unknown = target.model_copy(update={"source": None})
    assert locate_selection(SITE_FILES, unknown) == Selection(path=HERO_PATH, line=5, tag="a", text="Contact")
    assert locate_selection(SITE_FILES, None) is None


def test_the_first_context_is_the_selected_file_and_a_short_stylesheet() -> None:
    selection = Selection(path=HERO_PATH, line=5, tag="a", text="Contact")

    assert first_context(SITE_FILES, selection) == [
        ContextRef(path=HERO_PATH, start=1, end=8),
        ContextRef(path="src/index.css", start=1, end=3),
    ]
    # Nothing selected: the entry file.
    assert first_context(SITE_FILES, None)[0] == ContextRef(path="src/App.tsx", start=1, end=9)


def test_a_long_file_is_shown_as_its_outline_and_the_lines_around_the_selection() -> None:
    files = {"src/Big.tsx": "import x from 'y'\n" + "const a = 1\n" * 3000}

    refs = first_context(files, Selection(path="src/Big.tsx", line=1000, tag="div", text=""))

    assert refs == [
        ContextRef(path="src/Big.tsx", start=1, end=3001, outline=True),
        ContextRef(path="src/Big.tsx", start=1000 - CONTEXT_WINDOW_LINES, end=1000 + CONTEXT_WINDOW_LINES),
    ]
    assert outline(files["src/Big.tsx"], "src/Big.tsx")[0] == (1, "import x from 'y'")


def test_requested_code_is_capped_and_missing_files_are_explained() -> None:
    files = {"src/App.tsx": "x\n" * 2000, "src/a.css": "a {}\n"}

    refs, note = requested_context(files, ContextRequest(path="./src/App.tsx", lines=[10, 20]), [])
    assert refs == [ContextRef(path="src/App.tsx", start=10, end=20)]
    assert "lines 10-20" in note

    used = [ContextRef(path="src/App.tsx", start=1, end=MAX_EXTRA_CONTEXT_LINES - 5)]
    refs, note = requested_context(files, ContextRequest(path="src/App.tsx", lines=[100, 200]), used)
    assert refs == [ContextRef(path="src/App.tsx", start=100, end=104)]
    assert "Only lines" in note

    assert requested_context(files, ContextRequest(path="src/Nope.tsx"), [])[0] == []
    assert requested_context(files, ContextRequest(path="src/a.css"), [])[0] == [
        ContextRef(path="src/a.css", start=1, end=1)
    ]
    assert HERO.count("\n") == 8
