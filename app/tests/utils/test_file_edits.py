"""Mirrors `@makable/shared` `ai-edit-files.test.ts`: the SPA applies the same edits with the TypeScript original."""

import pytest

from app.schemas.ai_edit import FileEdit
from app.utils.file_edits import FileEditError, apply_file_edits

FILES = {
    "src/Hero.tsx": 'export function Hero() {\n  return (\n    <a className="bg-blue-600">Contact</a>\n  )\n}\n',
    "src/index.css": "a { color: red; }\na { color: red; }\n",
}


def edit(path: str, search: str, replace: str) -> FileEdit:
    return FileEdit(path=path, search=search, replace=replace)


def test_replaces_an_exact_unique_match_without_touching_the_input() -> None:
    result = apply_file_edits(FILES, [edit("src/Hero.tsx", "bg-blue-600", "bg-emerald-600")])

    assert result.files == {**FILES, "src/Hero.tsx": FILES["src/Hero.tsx"].replace("blue", "emerald")}
    assert result.changed == ["src/Hero.tsx"]
    assert "bg-blue-600" in FILES["src/Hero.tsx"]


def test_falls_back_to_a_line_match_that_ignores_indentation() -> None:
    search = '\t<a className="bg-blue-600">Contact</a>\n'
    result = apply_file_edits(FILES, [edit("src/Hero.tsx", search, '    <a className="bg-blue-600">Hire me</a>')])
    assert (
        result.files["src/Hero.tsx"]
        == 'export function Hero() {\n  return (\n    <a className="bg-blue-600">Hire me</a>\n  )\n}\n'
    )

    indented = apply_file_edits(
        FILES, [edit("src/Hero.tsx", 'return (\n<a className="bg-blue-600">Contact</a>', "return (\n    <b />")]
    )
    assert "return (\n    <b />\n  )" in indented.files["src/Hero.tsx"]


@pytest.mark.parametrize(
    ("edits", "error"),
    [
        ([edit("src/Hero.tsx", "nope", "x")], "not found"),
        ([edit("src/index.css", "a { color: red; }", "x")], "more than once"),
        ([edit("src/App.tsx", "a", "b")], "wasn't sent"),
        ([edit("src/Hero.tsx", "Contact", "Hi"), edit("src/Hero.tsx", "Contact", "Again")], "edit 2"),
    ],
)
def test_rejects_missing_ambiguous_and_unknown_file_edits(edits: list[FileEdit], error: str) -> None:
    with pytest.raises(FileEditError, match=error):
        apply_file_edits(FILES, edits)


def test_later_edits_see_earlier_ones_and_no_op_edits_are_not_changes() -> None:
    result = apply_file_edits(
        FILES, [edit("src/Hero.tsx", "Contact", "Hire me"), edit("src/Hero.tsx", "Hire me", "Say hi")]
    )
    assert ">Say hi<" in result.files["src/Hero.tsx"]

    assert apply_file_edits(FILES, [edit("src/Hero.tsx", "Contact", "Contact")]).changed == []


def test_the_size_cap_counts_like_javascript() -> None:
    # An emoji is one code point in Python but two UTF-16 code units in JavaScript.
    files = {"a.txt": "x"}
    with pytest.raises(FileEditError, match="too large"):
        apply_file_edits(files, [edit("a.txt", "x", "😀" * 30_001)])
