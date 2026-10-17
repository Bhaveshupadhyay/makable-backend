"""The request as the model reads it: earlier requests, the instruction, the selection, the site's file list and the
code it's shown, all as tagged data. The code is read from the snapshot each time (the state only says which lines),
and shown as numbered lines."""

from collections.abc import Iterable

from app.agents.ai_edit.state import ContextRef, Selection
from app.agents.ai_edit.tools.context import line_count, outline
from app.agents.thread import Turn
from app.constants.ai_edit import (
    MAX_HISTORY,
    MAX_HISTORY_REPLY,
    MAX_LISTED_FILES,
    MAX_SHOWN_CHARS,
    MAX_SHOWN_LINE_CHARS,
)
from app.constants.site import RESERVED_PATH
from app.schemas.ai_edit import AiEditRequest, AiEditTarget, AiEditTurn

WHOLE_SITE = "<selected_element>none: the request is about the whole site</selected_element>"


def build_user_prompt(
    request: AiEditRequest,
    selection: Selection | None,
    files: dict[str, str],
    context: list[ContextRef],
    history: list[AiEditTurn],
) -> str:
    """The first prompt: earlier requests, the instruction, the selection, the file list and the code it starts with."""
    parts = [
        *([_describe_history(history)] if history else []),
        f"<instruction>\n{request.instruction}\n</instruction>",
        _describe_target(request.target, selection) if request.target else WHOLE_SITE,
        _file_list(files),
        *(render_ref(files, ref) for ref in context),
    ]
    return "\n\n".join(parts)


def render_ref(files: dict[str, str], ref: ContextRef) -> str:
    """Some lines of a file, numbered, or its outline, within `MAX_SHOWN_CHARS`."""
    text = files.get(ref["path"], "")
    total = line_count(text)
    path = _attr(ref["path"])
    if ref.get("outline"):
        lines, _ = _shown(outline(text, ref["path"]))
        return f'<file_outline path="{path}" total_lines="{total}">\n{lines}\n</file_outline>'
    start, end = ref["start"], min(ref["end"], total)
    body, last = _shown(enumerate(text.split("\n")[start - 1 : end], start=start))
    return f'<file path="{path}" lines="{start}-{last or end}" total_lines="{total}">\n{body}\n</file>'


def _shown(lines: Iterable[tuple[int, str]]) -> tuple[str, int]:
    """Numbered lines, each cut after `MAX_SHOWN_LINE_CHARS` (marked as cut, since a search must stop before the
    mark), until they'd pass `MAX_SHOWN_CHARS`; and the last line shown."""
    shown: list[str] = []
    size = last = 0
    for number, line in lines:
        if len(line) > MAX_SHOWN_LINE_CHARS:
            line = f"{line[:MAX_SHOWN_LINE_CHARS]}⟪cut: {len(line) - MAX_SHOWN_LINE_CHARS} more characters⟫"
        numbered = _numbered(number, line)
        if shown and size + len(numbered) > MAX_SHOWN_CHARS:
            break
        shown.append(numbered)
        size += len(numbered) + 1
        last = number
    return "\n".join(shown), last


def earlier_requests(turns: list[Turn]) -> list[AiEditTurn]:
    """The thread's last turns, for the prompt's `<earlier_requests>`."""
    return [
        AiEditTurn(instruction=t["instruction"], target=t["target"], reply=_reply(t)[:MAX_HISTORY_REPLY])
        for t in turns[-MAX_HISTORY:]
    ]


def _reply(turn: Turn) -> str:
    asked = "".join(f" (You asked: {q['question']} They said: {q['answer']})" for q in turn.get("questions", []))
    return turn["reply"] + asked


def _file_list(files: dict[str, str]) -> str:
    """The site's text files, without makable's and git's own (the model can't change those)."""
    paths = sorted(path for path in files if not RESERVED_PATH.match(path))
    listed = "\n".join(f"{path} ({line_count(files[path])} lines)" for path in paths[:MAX_LISTED_FILES])
    more = f"\n... and {len(paths) - MAX_LISTED_FILES} more" if len(paths) > MAX_LISTED_FILES else ""
    return f"<file_list>\n{listed}{more}\n</file_list>"


def _describe_target(target: AiEditTarget, selection: Selection | None) -> str:
    lines = [f"- Element: <{target.tag}>{f' showing "{target.text}"' if target.text else ''}"]
    if selection is not None:
        lines.append(f"- In the code: {selection['path']}, the element starting at line {selection['line']}")
    else:
        lines.append("- In the code: not found (it may be made by a script); find it from the outline and HTML")
    if target.section:
        section = target.section
        id_attr = f' id="{_attr(section.id)}"' if section.id else ""
        heading = f' with heading "{section.heading}"' if section.heading else ""
        lines.append(f"- Inside: <{section.tag}{id_attr}>{heading}")
    lines.append(f"- DOM path: {target.selector}")
    return "<selected_element>\n" + "\n".join(lines) + f"\n<html>\n{target.html}\n</html>\n</selected_element>"


def _describe_history(history: list[AiEditTurn]) -> str:
    entries = []
    for i, turn in enumerate(history, start=1):
        target = f" (selected: {turn.target})" if turn.target else ""
        entries.append(f"{i}. User{target}: {turn.instruction}\n   Result: {turn.reply}")
    return "<earlier_requests>\n" + "\n".join(entries) + "\n</earlier_requests>"


def _numbered(number: int, line: str) -> str:
    return f"{number:>4}| {line}"


def _attr(value: str) -> str:
    return value.translate({ord(c): None for c in '"<>&'})
