import json
from typing import Any

import pytest

from app.schemas.ai_edit import AiEditRequest, FileEdit, ModelEdits
from app.services.ai_edit_repair import MAX_REPORT_CHARS, rejection_report
from app.services.ai_edit_service import check_edits, extract_json
from app.tests.ai_edit_fixtures import CONTENT_PATH, HERO_PATH, PORTFOLIO, make_request, render_portfolio_source
from app.utils.file_edits import FileEditError

BUNDLE_PATH = "assets/MainContainer-CNANTDSP.js"
BUNDLE = (
    'import{j as e}from"./react.js";const d=()=>e.jsx("div",{}),E=()=>e.jsx("p",{children:"x"})})),'
    'q=[[{name:"JavaScript",icon:"js.svg"},{name:"HTML",icon:"html.svg"}]],'
    'I=()=>e.jsx("h2",{children:"Tech Stack"});export{I as T};\n'
)


def hublend_request() -> AiEditRequest:
    files: list[dict[str, Any]] = [
        {"path": BUNDLE_PATH, "content": BUNDLE, "reason": "matches the selection"},
        {"path": "content/portfolio.js", "content": render_portfolio_source(PORTFOLIO), "reason": "content file"},
    ]
    template = {"id": "hublend", "name": "Hublend", "kind": "static", "version": 2}
    template["contentPath"] = "content/portfolio.js"
    return make_request(files=files, template=template, fileTree=[f["path"] for f in files])


def report_for(request: AiEditRequest, edits: list[FileEdit]) -> str:
    with pytest.raises(FileEditError) as caught:
        check_edits(request, edits)
    return rejection_report(request, edits, caught.value)


def test_a_missed_search_in_minified_code_shows_both_texts_and_an_anchor() -> None:
    # The real failure: the model's first edit was right; its second added `const ` where the file has `,`.
    request = hublend_request()
    edits = [
        FileEdit(path="content/portfolio.js", search='"skills": [\n    "TypeScript"\n  ]', replace='"skills": ["Go"]'),
        FileEdit(path=BUNDLE_PATH, search='const q=[[{name:"JavaScript",icon:"js.svg"}', replace="const q=[["),
    ]

    report = report_for(request, edits)

    assert report.startswith(f"Edit 2 of 2 ({BUNDLE_PATH}) was rejected: the search text was not found")
    assert "Edits before it applied cleanly: 1 (content/portfolio.js). Keep them exactly as they are." in report
    assert '⟦const q=[[{name:"JavaScript",icon:"js.svg"}⟧' in report
    assert "The end of your search text (37 characters) is in the file at line 1, column 95" in report
    assert '  You wrote:    ⟦const q=[[{name:"JavaScript",icon:"j⟧' in report
    assert '  The file has: ⟦})})),q=[[{name:"JavaScript",icon:"j⟧' in report
    assert 'it can anchor your search: ⟦q=[[{name:"J⟧' in report


def test_a_search_in_the_wrong_file_says_which_file_has_it() -> None:
    request = hublend_request()

    report = report_for(request, [FileEdit(path=BUNDLE_PATH, search='"headline": "Engineer"', replace='"x"')])

    assert "That text is in content/portfolio.js, not in assets/MainContainer-CNANTDSP.js." in report


def test_a_search_like_nothing_in_the_file_says_so() -> None:
    report = report_for(make_request(), [FileEdit(path=HERO_PATH, search="function Footer() {}", replace="")])

    assert f"No part of it (not even its first or last 12 characters) is in {HERO_PATH}." in report


def test_an_ambiguous_search_lists_every_place_it_matches() -> None:
    request = make_request(
        files=[{"path": "src/index.css", "content": "a { color: red; }\nb { color: red; }\n", "reason": "styles"}]
    )

    report = report_for(request, [FileEdit(path="src/index.css", search="color: red;", replace="color: blue;")])

    assert "Your search text appears 2 times:" in report
    assert "- at line 1, column 5: ⟦a { color: red; }" in report
    assert "- at line 2, column 5:" in report
    assert "Extend the search with text that is next to the one you mean" in report


def test_a_broken_script_points_at_the_error_in_the_edited_file() -> None:
    report = report_for(make_request(), [FileEdit(path=HERO_PATH, search="</a>", replace="")])

    assert report.startswith(f"All 1 edits applied, but the result was rejected: {HERO_PATH}: syntax error")
    assert f"The edits to {HERO_PATH} are: 1. One of them breaks the file." in report
    assert "The problem is at line 2, column" in report
    assert "⟦export function Hero()" in report


def test_a_broken_content_file_restates_its_format() -> None:
    report = report_for(make_request(), [FileEdit(path=CONTENT_PATH, search='"Engineer"', replace="'Engineer',")])

    assert "The content file is a JSON object literal: double quotes, no trailing commas" in report


def test_edits_that_change_nothing_and_forbidden_files_are_explained() -> None:
    same = FileEdit(path=HERO_PATH, search="bg-blue-600", replace="bg-blue-600")
    assert "leave every file unchanged" in report_for(make_request(), [same])

    lockfile = FileEdit(path="package.json", search="a", replace="b")
    assert "Edit 2 of 2 (package.json) was rejected" in report_for(make_request(), [same, lockfile])
    assert "can't be edited at all. Drop this edit." in report_for(make_request(), [same, lockfile])


def test_replies_that_are_not_answers_are_explained() -> None:
    with pytest.raises(ValueError) as not_json:
        extract_json("Sure, here you go")
    assert "couldn't be read as JSON (The model did not return JSON)" in rejection_report(
        make_request(), None, not_json.value
    )

    with pytest.raises(ValueError) as wrong_shape:
        ModelEdits.model_validate({"summary": "x"})
    report = rejection_report(make_request(), None, wrong_shape.value)
    assert report.startswith("Your reply was JSON, but not in the answer format: edits: Field required.")


def test_the_report_is_capped() -> None:
    search = json.dumps("x" * 10_000)
    report = report_for(make_request(), [FileEdit(path=HERO_PATH, search=search, replace="")])

    assert len(report) <= MAX_REPORT_CHARS


def test_one_edit_that_changes_nothing_next_to_real_edits_is_named() -> None:
    real = FileEdit(path=HERO_PATH, search="bg-blue-600", replace="bg-emerald-600")
    same = FileEdit(path=HERO_PATH, search="Contact", replace="Contact")

    report = report_for(make_request(), [real, same])

    assert report.startswith(f"Edit 2 of 2 ({HERO_PATH}) was rejected: its search and replace are the same")
    assert f"Edits before it applied cleanly: 1 ({HERO_PATH})." in report
    assert "If this part needs no change, drop the edit" in report


def test_a_stylesheet_with_a_stray_brace_points_at_it() -> None:
    report = report_for(make_request(), [FileEdit(path="src/index.css", search="}", replace="}\n}")])

    assert "src/index.css: unbalanced braces: a } that closes nothing" in report
    assert "The problem is at line 2, column 1 of the edited file:" in report
