"""Search/replace edits on text files: a port of `applyFileEdits` from `@makable/shared` `ai-edit-files.ts`.

The model's edits are checked here before they're sent, and the SPA applies them again with the TypeScript
original, so the two must agree. Keep them in sync (the tests mirror `ai-edit-files.test.ts`).
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from app.constants.ai_edit import MAX_FILE_CHARS
from app.schemas.ai_edit import FileEdit


class FileEditError(ValueError):
    """An edit can't be applied. The message is written for the model, which gets it back on a retry."""


@dataclass(frozen=True)
class AppliedEdits:
    files: dict[str, str]
    # Paths whose text actually changed, in the order they were first changed.
    changed: list[str]


def apply_file_edits(files: Mapping[str, str], edits: Sequence[FileEdit]) -> AppliedEdits:
    """Applies edits in order (later edits see earlier ones) to a copy of `files`. All or nothing.

    An exact match comes first; otherwise the search is compared line by line ignoring leading and trailing
    whitespace, because models often get indentation wrong. Either way the match must be unique, so an edit
    can never land in the wrong place.

    Raises:
        FileEditError: An edit targets a file that wasn't given, its search text isn't found exactly once,
            or the file would get too large.
    """
    result = dict(files)
    changed: dict[str, None] = {}
    for i, edit in enumerate(edits, start=1):
        where = f"edit {i} ({edit.path})"
        if edit.path not in result:
            raise FileEditError(f"{where}: that file wasn't sent, so it can't be edited")
        try:
            text = _replace_once(result[edit.path], edit.search, edit.replace)
        except FileEditError as err:
            raise FileEditError(f"{where}: {err}") from None
        if _js_length(text) > MAX_FILE_CHARS:
            raise FileEditError(f"{where}: the file would be too large")
        if text != files[edit.path]:
            changed[edit.path] = None
        result[edit.path] = text
    return AppliedEdits(files=result, changed=list(changed))


def _replace_once(text: str, search: str, replace: str) -> str:
    first = text.find(search)
    if first != -1:
        if text.find(search, first + 1) != -1:
            raise FileEditError("the search text appears more than once; include more surrounding lines")
        return text[:first] + replace + text[first + len(search) :]
    # Line-based fallback: whole lines, compared trimmed. Blank edge lines in the search are ignored.
    want = [line.strip() for line in _trim_blank_edges(search.split("\n"))]
    if not want:
        raise FileEditError("the search text is empty")
    lines = text.split("\n")
    starts = [
        s for s in range(len(lines) - len(want) + 1) if all(lines[s + k].strip() == w for k, w in enumerate(want))
    ]
    if not starts:
        raise FileEditError("the search text was not found; copy it exactly from the file")
    if len(starts) > 1:
        raise FileEditError("the search text appears more than once; include more surrounding lines")
    s = starts[0]
    return "\n".join([*lines[:s], *_trim_blank_edges(replace.split("\n")), *lines[s + len(want) :]])


def _trim_blank_edges(lines: list[str]) -> list[str]:
    a, b = 0, len(lines)
    while a < b and not lines[a].strip():
        a += 1
    while b > a and not lines[b - 1].strip():
        b -= 1
    return lines[a:b]


def _js_length(text: str) -> int:
    """`String.length` in JavaScript (UTF-16 code units), so the size cap matches the browser's."""
    return len(text.encode("utf-16-le")) // 2
