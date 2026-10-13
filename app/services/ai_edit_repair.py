"""The report sent back to the model after a rejected answer: which edit failed, why and where, with the
file's real text at that place. It goes inside the repair prompt (`ai_edit_prompt.repair_prompt`)."""

from pydantic import ValidationError

from app.schemas.ai_edit import AiEditRequest, FileEdit
from app.utils.edit_diagnosis import MIN_MATCH, excerpt, line_col, near_match, occurrences, unique_anchor
from app.utils.file_edits import FileEditError, apply_file_edits

# The report is cut here, so a long one can't crowd the files out of the model's context.
MAX_REPORT_CHARS = 6000
# Characters of the model's own search text quoted back to it.
QUOTED_SEARCH = 300


def rejection_report(request: AiEditRequest, edits: list[FileEdit] | None, err: Exception) -> str:
    """Explains a rejected answer to the model. `edits` is None when the reply wasn't a valid answer at all."""
    if not isinstance(err, FileEditError) or edits is None:
        report = _invalid_answer(err)
    elif err.edit is not None and 1 <= err.edit <= len(edits):
        report = _failed_edit(request, edits, err.edit, err)
    else:
        report = _whole_answer(request, edits, err)
    return report if len(report) <= MAX_REPORT_CHARS else report[: MAX_REPORT_CHARS - 1] + "…"


def _invalid_answer(err: Exception) -> str:
    if isinstance(err, ValidationError):
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc']) or 'the answer'}: {e['msg']}" for e in err.errors()[:5]
        )
        return (
            f"Your reply was JSON, but not in the answer format: {problems}.\n"
            'It must be {"summary": "...", "edits": [{"path": "...", "search": "...", "replace": "..."}]} '
            'or {"escalate": "..."}.'
        )
    return (
        f"Your reply couldn't be read as JSON ({err}). Reply with exactly one JSON object; escape newlines in "
        'strings as \\n and double quotes as \\".'
    )


def _failed_edit(request: AiEditRequest, edits: list[FileEdit], index: int, err: FileEditError) -> str:
    edit = edits[index - 1]
    lines = [f"Edit {index} of {len(edits)} ({edit.path}) was rejected: {_reason(err)}."]
    if index > 1:
        done = ", ".join(f"{i} ({e.path})" for i, e in enumerate(edits[: index - 1], start=1))
        lines.append(f"Edits before it applied cleanly: {done}. Keep them exactly as they are.")
    if index < len(edits):
        lines.append(f"Edits after it ({index + 1}-{len(edits)}) weren't checked yet.")
    # The file as the failing edit saw it: the earlier edits applied.
    files = apply_file_edits({f.path: f.content for f in request.files}, edits[: index - 1]).files
    text = files.get(edit.path)
    lines.append("")
    if err.kind == "not_found" and text is not None:
        lines += _not_found(files, edit, text)
    elif err.kind == "ambiguous" and text is not None:
        lines += _ambiguous(edit, text)
    elif err.kind == "file_not_sent":
        sent = ", ".join(f.path for f in request.files)
        lines.append(f"Only the files you were given can be edited: {sent}. Edit one of those, or escalate.")
    elif err.kind == "empty_search":
        lines.append('"search" must contain text that is already in the file.')
    elif err.kind == "too_large":
        lines.append("The edit makes the file too large. Replace a smaller part of it.")
    elif err.kind == "forbidden":
        lines.append(f"{edit.path} can't be edited at all. Drop this edit.")
    elif err.kind == "no_change":
        lines.append(
            "Its search and replace are identical. If this part needs no change, drop the edit; otherwise put the "
            "new text in replace. Every edit you send must change the file."
        )
    return "\n".join(lines)


def _not_found(files: dict[str, str], edit: FileEdit, text: str) -> list[str]:
    lines = [f"Your search text ({len(edit.search)} characters):", _quote(edit.search[:QUOTED_SEARCH])]
    near = near_match(text, edit.search)
    if near is None:
        elsewhere = [path for path, other in files.items() if path != edit.path and edit.search in other]
        if elsewhere:
            lines.append(f"That text is in {', '.join(elsewhere)}, not in {edit.path}. Edit the file that has it.")
        else:
            lines.append(
                f"No part of it (not even its first or last {MIN_MATCH} characters) is in {edit.path}. Find the code "
                "you mean in the <file> block and copy it from there."
            )
        return lines
    which = "start" if near.from_start else "end"
    lines += [
        f"The {which} of your search text ({near.length} characters) is in the file at "
        f"{line_col(text, near.offset)}, but the {'rest' if near.from_start else 'text before it'} differs:",
        f"  You wrote:    {_quote(near.wrote)}",
        f"  The file has: {_quote(near.file_has)}",
        "The file around that place:",
        _quote(excerpt(text, near.offset, near.offset + near.length)),
    ]
    if near.anchor:
        lines.append(f"This text appears once in the file, so it can anchor your search: {_quote(near.anchor)}")
    return lines


def _ambiguous(edit: FileEdit, text: str) -> list[str]:
    places = occurrences(text, edit.search)
    if not places:  # Matched more than once only line by line (ignoring indentation).
        return ["It matches several places when indentation is ignored. Include a neighbouring line that is unique."]
    lines = [f"Your search text appears {'at least ' if len(places) == 5 else ''}{len(places)} times:"]
    for at in places:
        anchor = unique_anchor(text, max(0, at - 40))
        lines.append(f"- at {line_col(text, at)}: {_quote(excerpt(text, at, at + len(edit.search)))}")
        if anchor:
            lines.append(f"  unique text just before it: {_quote(anchor)}")
    lines.append("Extend the search with text that is next to the one you mean and nowhere else.")
    return lines


def _whole_answer(request: AiEditRequest, edits: list[FileEdit], err: FileEditError) -> str:
    """Problems with the result of all the edits together: nothing changed, or a file no longer parses."""
    if err.kind == "no_change":
        return (
            "Your edits leave every file unchanged (each replace equals its search, or they cancel out). "
            "Make the change the user asked for, or escalate if you can't."
        )
    lines = [f"All {len(edits)} edits applied, but the result was rejected: {err}."]
    if err.path is not None:
        touching = ", ".join(str(i) for i, e in enumerate(edits, start=1) if e.path == err.path)
        lines.append(f"The edits to {err.path} are: {touching}. One of them breaks the file.")
        text = apply_file_edits({f.path: f.content for f in request.files}, edits).files.get(err.path)
        if text is not None and err.offset is not None:
            lines += [
                f"The problem is at {line_col(text, err.offset)} of the edited file:",
                _quote(excerpt(text, err.offset)),
            ]
        if err.path == request.template.content_path:
            lines.append(
                "The content file is a JSON object literal: double quotes, no trailing commas, no comments, "
                "and the same keys as before."
            )
    return "\n".join(lines)


def _reason(err: FileEditError) -> str:
    """The error without its `edit N (path): ` prefix, which the report already says."""
    message = str(err)
    return message.split("): ", 1)[1] if message.startswith("edit ") and "): " in message else message


def _quote(text: str) -> str:
    """Quotes file text so its edges are visible: the model must copy it exactly, spaces included."""
    return "⟦" + text + "⟧"
