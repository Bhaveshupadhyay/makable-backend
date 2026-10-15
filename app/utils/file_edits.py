"""Search/replace edits on text files, first ported from `applyFileEdits` in `@makable/shared` `ai-edit-files.ts`.

The server applies the model's edits itself now (it dry-runs them, then commits the result), and sends the SPA the
files they produce, so only this version runs. It also creates files: an edit with an empty search on a path that
doesn't exist yet.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from app.constants.ai_edit import MAX_EDITED_FILE_CHARS
from app.schemas.ai_edit import FileEdit

EditProblem = Literal[
    "file_not_found", "not_found", "ambiguous", "empty_search", "too_large", "forbidden", "no_change", "broken_file"
]


class FileEditError(ValueError):
    """An edit can't be applied. The message is written for the model, which gets it back on a retry.

    The other fields say what went wrong where, so the retry can explain it in detail: `edit` is the 1-based
    index of the failing edit, `path` its file, and `offset` the place in the file it concerns, if known.
    """

    def __init__(
        self,
        message: str,
        *,
        kind: EditProblem | None = None,
        edit: int | None = None,
        path: str | None = None,
        offset: int | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.edit = edit
        self.path = path
        self.offset = offset


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

    An empty search creates the file with `replace` as its text, if there's no such file yet.

    Raises:
        FileEditError: An edit targets a file that doesn't exist (with a search), its search text isn't found
            exactly once, or the file would get too large.
    """
    result = dict(files)
    changed: dict[str, None] = {}
    for i, edit in enumerate(edits, start=1):
        where = f"edit {i} ({edit.path})"
        if edit.path not in result:
            if edit.search:
                raise FileEditError(
                    f"{where}: there's no such file; to create it, send an empty search",
                    kind="file_not_found",
                    edit=i,
                    path=edit.path,
                )
            text = edit.replace
        elif not edit.search:
            raise FileEditError(
                f"{where}: the file exists, so the search must be text from it",
                kind="empty_search",
                edit=i,
                path=edit.path,
            )
        else:
            try:
                text = _replace_once(result[edit.path], edit.search, edit.replace)
            except FileEditError as err:
                raise FileEditError(f"{where}: {err}", kind=err.kind, edit=i, path=edit.path) from None
        if _js_length(text) > MAX_EDITED_FILE_CHARS:
            raise FileEditError(f"{where}: the file would be too large", kind="too_large", edit=i, path=edit.path)
        if text != files.get(edit.path):
            changed[edit.path] = None
        result[edit.path] = text
    return AppliedEdits(files=result, changed=list(changed))


def _replace_once(text: str, search: str, replace: str) -> str:
    ambiguous = "the search text appears more than once; include more surrounding lines"
    first = text.find(search)
    if first != -1:
        if text.find(search, first + 1) != -1:
            raise FileEditError(ambiguous, kind="ambiguous")
        return text[:first] + replace + text[first + len(search) :]
    # Line-based fallback: whole lines, compared trimmed. Blank edge lines in the search are ignored.
    want = [line.strip() for line in _trim_blank_edges(search.split("\n"))]
    if not want:
        raise FileEditError("the search text is empty", kind="empty_search")
    lines = text.split("\n")
    starts = [
        s for s in range(len(lines) - len(want) + 1) if all(lines[s + k].strip() == w for k, w in enumerate(want))
    ]
    if not starts:
        raise FileEditError("the search text was not found; copy it exactly from the file", kind="not_found")
    if len(starts) > 1:
        raise FileEditError(ambiguous, kind="ambiguous")
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
