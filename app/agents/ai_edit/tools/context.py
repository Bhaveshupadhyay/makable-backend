"""What the model sees of the site's code: where the selected element is, which lines of which files go into the
first prompt, outlines of long files, and the extra code the model asks for (`needContext`). Only references (path
and lines) go into the agent's state; the text is read from the snapshot when the prompt is built."""

import re

from app.agents.ai_edit.state import ContextRef, Selection
from app.constants.ai_edit import (
    CONTEXT_WINDOW_LINES,
    MAX_EXTRA_CONTEXT_LINES,
    MAX_OUTLINE_LINES,
    SAFE_REPO_PATH,
    WHOLE_FILE_CHARS,
)
from app.schemas.ai_edit import AiEditTarget, ContextRequest

# Where a site's code starts, most likely first.
ENTRY_FILES = ("src/App.tsx", "src/App.jsx", "src/main.tsx", "src/main.jsx", "index.html")
# Stylesheets shown with the first prompt when they're short, for changes of colour, font or spacing.
STYLESHEETS = ("src/index.css", "src/App.css", "styles.css", "style.css", "css/styles.css")
SHORT_STYLESHEET_CHARS = 6_000
# Files with markup the preview tags (a selection can only point into these).
MARKUP = re.compile(r"\.(tsx|jsx|html?)$")
# Lines that make a file's outline: imports and declarations (JS/TS), selectors (CSS), landmarks (HTML).
_SCRIPT_OUTLINE = re.compile(r"^\s*(import|export|function|async function|const|let|class|interface|type)\b")
_CSS_OUTLINE = re.compile(r"^[^\s{}][^{}]*\{")
_HTML_OUTLINE = re.compile(r"<(header|nav|main|section|article|aside|footer|h1|h2|form)\b|\sid=", re.IGNORECASE)


def line_count(text: str) -> int:
    return text.count("\n") + (0 if text.endswith("\n") else 1) if text else 1


def locate_selection(files: dict[str, str], target: AiEditTarget | None) -> Selection | None:
    """The selected element's file and line. The preview's `data-src` is the place, if the file has the element's tag
    there; otherwise the nearest line with the tag (and its text) around it, or the one place its text is written in
    the site's markup. None when the element can't be found in the code (the model then works from the outline)."""
    if target is None:
        return None
    tag = target.tag.lower()
    source = target.source
    if source is not None and source.path in files:
        lines = files[source.path].split("\n")
        line = min(max(source.line, 1), len(lines))
        found = _nearest_tag_line(lines, line, tag, target.text)
        return Selection(path=source.path, line=found or line, tag=tag, text=target.text)
    return _find_by_text(files, tag, target.text)


def first_context(files: dict[str, str], selection: Selection | None) -> list[ContextRef]:
    """The code in the first prompt: the selected file (whole if it's short, else its outline and the lines around
    the selection), or the site's entry file when nothing is selected, plus a short main stylesheet."""
    path = selection["path"] if selection else entry_file(files)
    refs: list[ContextRef] = []
    if path is not None:
        refs += _file_refs(path, files[path], selection["line"] if selection else 1)
    sheet = next((p for p in STYLESHEETS if p in files and p != path), None)
    if sheet is not None and len(files[sheet]) <= SHORT_STYLESHEET_CHARS:
        refs.append(ContextRef(path=sheet, start=1, end=line_count(files[sheet])))
    return refs


def entry_file(files: dict[str, str]) -> str | None:
    found = next((p for p in ENTRY_FILES if p in files), None)
    return found or next((p for p in sorted(files) if MARKUP.search(p)), None)


def requested_context(
    files: dict[str, str], request: ContextRequest, already: list[ContextRef]
) -> tuple[list[ContextRef], str]:
    """The code the model asked for, within what's left of `MAX_EXTRA_CONTEXT_LINES` after what it asked for
    before (`already`), and a note for it: what it gets, or why it gets less (or nothing)."""
    path = request.path.strip().removeprefix("./").removeprefix("/")
    if not SAFE_REPO_PATH.fullmatch(path) or path not in files:
        return [], f"There's no text file {path!r} in the site. The files are in <file_list>."
    used = sum(r["end"] - r["start"] + 1 for r in already if not r.get("outline"))
    left = MAX_EXTRA_CONTEXT_LINES - used
    if left <= 0:
        return [], "No more code can be shown for this request. Answer with what you have."
    total = line_count(files[path])
    if request.lines is not None:
        start, end = sorted(request.lines)
        start, end = max(1, start), min(total, end)
        if start > end:
            return [], f"{path} has {total} lines; lines {request.lines[0]}-{request.lines[1]} aren't in it."
    elif len(files[path]) <= WHOLE_FILE_CHARS:
        start, end = 1, total
    else:
        outline = ContextRef(path=path, start=1, end=total, outline=True)
        return [outline], f"{path} is long ({total} lines): here's its outline. Ask for the lines you need."
    if end - start + 1 > left:
        end = start + left - 1
        return [
            ContextRef(path=path, start=start, end=end)
        ], f"Only lines {start}-{end} fit: no more code can be shown."
    return [ContextRef(path=path, start=start, end=end)], f"Here is {path}, lines {start}-{end}."


def outline(text: str, path: str) -> list[tuple[int, str]]:
    """A file's outline: (line number, line) for its imports and declarations, CSS selectors or HTML landmarks."""
    if path.endswith(".css"):
        pattern = _CSS_OUTLINE
    elif re.search(r"\.html?$", path):
        pattern = _HTML_OUTLINE
    else:
        pattern = _SCRIPT_OUTLINE
    found = [(i, line.rstrip()) for i, line in enumerate(text.split("\n"), start=1) if pattern.search(line)]
    return found[:MAX_OUTLINE_LINES]


def _file_refs(path: str, text: str, line: int) -> list[ContextRef]:
    total = line_count(text)
    if len(text) <= WHOLE_FILE_CHARS:
        return [ContextRef(path=path, start=1, end=total)]
    start, end = max(1, line - CONTEXT_WINDOW_LINES), min(total, line + CONTEXT_WINDOW_LINES)
    return [ContextRef(path=path, start=1, end=total, outline=True), ContextRef(path=path, start=start, end=end)]


def _nearest_tag_line(lines: list[str], line: int, tag: str, text: str) -> int | None:
    """The line nearest `line` (within the context window) that opens a `tag` element, preferring one whose next few
    lines have the element's text."""
    opens = re.compile(rf"<{re.escape(tag)}(?=[\s>/])", re.IGNORECASE)
    words = text.strip()[:40]
    best: int | None = None
    for distance in range(CONTEXT_WINDOW_LINES + 1):
        for candidate in (line - distance, line + distance) if distance else (line,):
            if not 1 <= candidate <= len(lines) or not opens.search(lines[candidate - 1]):
                continue
            nearby = "\n".join(lines[candidate - 1 : candidate + 3])
            if not words or words in nearby:
                return candidate
            best = best or candidate
    return best


def _find_by_text(files: dict[str, str], tag: str, text: str) -> Selection | None:
    words = text.strip()[:40]
    if len(words) < 3:
        return None
    places = [
        (path, content[: content.index(words)].count("\n") + 1)
        for path, content in sorted(files.items())
        if MARKUP.search(path) and content.count(words) == 1
    ]
    if len(places) != 1:
        return None
    path, line = places[0]
    return Selection(path=path, line=line, tag=tag, text=text)
