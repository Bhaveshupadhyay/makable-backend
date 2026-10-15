"""The prompt after a rejected answer: `REPAIR_PROMPT` and a report of which edit failed, why and where, with the
file's real text at that place."""

from pydantic import ValidationError

from app.agents.ai_edit.prompts.system import REPAIR_PROMPT
from app.schemas.ai_edit import FileEdit
from app.utils.edit_diagnosis import (
    MIN_MATCH,
    count_occurrences,
    excerpt,
    interior_match,
    line_col,
    line_matches,
    near_match,
    occurrences,
    unique_anchor,
)
from app.utils.file_edits import FileEditError, apply_file_edits

# The report is cut here, so a long one can't crowd the files out of the model's context.
MAX_REPORT_CHARS = 6000
# Characters of the model's own search text quoted back to it.
QUOTED_SEARCH = 300


def repair_prompt(report: str) -> str:
    """Sent after a rejected answer: how to repair it, then what exactly went wrong (`rejection_report`)."""
    return f"{REPAIR_PROMPT}\n\n<rejection_report>\n{report}\n</rejection_report>"


def rejection_report(files: dict[str, str], edits: list[FileEdit] | None, err: Exception) -> str:
    """Explains a rejected answer to the model, from the site's files (at the request's base commit). `edits` is None
    when the reply wasn't a valid answer at all."""
    if not isinstance(err, FileEditError) or edits is None:
        report = _invalid_answer(err)
    elif err.edit is not None and 1 <= err.edit <= len(edits):
        report = _failed_edit(files, edits, err.edit, err)
    else:
        report = _whole_answer(files, edits, err)
    return report if len(report) <= MAX_REPORT_CHARS else report[: MAX_REPORT_CHARS - 1] + "…"


def _invalid_answer(err: Exception) -> str:
    if isinstance(err, ValidationError):
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc']) or 'the answer'}: {e['msg']}" for e in err.errors()[:5]
        )
        return (
            f"Your reply was JSON, but not in the answer format: {problems}.\n"
            'It must be {"summary": "...", "edits": [{"path": "...", "search": "...", "replace": "..."}]}, '
            '{"needContext": {"path": "...", "lines": [first, last]}} or {"escalate": "..."}.'
        )
    return (
        f"Your reply couldn't be read as JSON ({err}). Reply with exactly one JSON object; escape newlines in "
        'strings as \\n and double quotes as \\".'
    )


def _failed_edit(site_files: dict[str, str], edits: list[FileEdit], index: int, err: FileEditError) -> str:
    edit = edits[index - 1]
    lines = [f"Edit {index} of {len(edits)} ({edit.path}) was rejected: {_reason(err)}."]
    if err.kind == "forbidden":
        # Checked before any edit is applied, so nothing else is known about the others yet.
        lines.append(f"{edit.path} can't be edited at all. Drop this edit. The other edits weren't checked yet.")
        return "\n".join(lines)
    if index > 1:
        done = ", ".join(f"{i} ({e.path})" for i, e in enumerate(edits[: index - 1], start=1))
        lines.append(f"Edits before it applied cleanly: {done}. Keep them exactly as they are.")
    if index < len(edits):
        lines.append(f"Edits after it ({index + 1}-{len(edits)}) weren't checked yet.")
    # The file as the failing edit saw it: the earlier edits applied. They did apply (the check stopped at this
    # one), but a report must never fail, so fall back to the site's files.
    try:
        files = apply_file_edits(site_files, edits[: index - 1]).files
    except FileEditError:
        files = site_files
    text = files.get(edit.path)
    lines.append("")
    if err.kind == "not_found" and text is not None:
        lines += _not_found(files, edit, text)
    elif err.kind == "ambiguous" and text is not None:
        lines += _ambiguous(edit, text)
    elif err.kind == "file_not_found":
        lines.append(
            f"There's no file {edit.path} in the site (see <file_list>). Edit a file that exists, or create this one "
            'with an empty "search" and the whole file as "replace".'
        )
    elif err.kind == "empty_search":
        lines.append(f'{edit.path} already exists, so "search" must contain text that is already in it.')
    elif err.kind == "too_large":
        lines.append("The edit makes the file too large. Replace a smaller part of it.")
    elif err.kind == "no_change":
        lines.append(
            "Its search and replace are identical. If this part needs no change, drop the edit; otherwise put the "
            "new text in replace. Every edit you send must change the file."
        )
    return "\n".join(lines)


def _not_found(files: dict[str, str], edit: FileEdit, text: str) -> list[str]:
    lines = [f"Your search text ({len(edit.search)} characters):", _quote(edit.search[:QUOTED_SEARCH])]
    elsewhere = [path for path, other in files.items() if path != edit.path and edit.search in other]
    if elsewhere:
        lines.append(f"That exact text is in {', '.join(elsewhere)}, not in {edit.path}. Edit the file that has it.")
        return lines
    near = near_match(text, edit.search)
    if near is None:
        middle = interior_match(text, edit.search)
        if middle is None:
            lines.append(
                f"Neither its first nor its last {MIN_MATCH} characters, nor any {2 * MIN_MATCH}-character piece "
                f"of it, is in {edit.path}. Find the code you mean in the <file> block and copy it from there."
            )
            return lines
        piece = edit.search[middle.start : middle.start + middle.length]
        lines += [
            f"A piece from the middle of your search text ({middle.length} characters, from character "
            f"{middle.start + 1}) is in the file at {line_col(text, middle.offset)}; the text around it differs:",
            f"  That piece: {_quote(piece[:QUOTED_SEARCH])}",
            "The file around that place:",
            _quote(excerpt(text, middle.offset, middle.offset + middle.length)),
        ]
        anchor = unique_anchor(text, middle.offset)
        if anchor:
            lines.append(f"This text appears once in the file, so it can anchor your search: {_quote(anchor)}")
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
    exact = bool(places)
    if exact:
        total = count_occurrences(text, edit.search)
    else:  # No exact match: it matched several places line by line, ignoring indentation.
        found = line_matches(text, edit.search)
        total, places = len(found), found[:5]
    how = "" if exact else " when indentation is ignored"
    shown = f", the first {len(places)} shown" if total > len(places) else ""
    lines = [f"Your search text matches {total} places{how}{shown}:"]
    for at in places:
        anchor = unique_anchor(text, max(0, at - 40))
        lines.append(f"- at {line_col(text, at)}: {_quote(excerpt(text, at, at + len(edit.search)))}")
        if anchor:
            lines.append(f"  unique text just before it: {_quote(anchor)}")
    lines.append("Extend the search with text that is next to the one you mean and nowhere else.")
    return lines


def _whole_answer(site_files: dict[str, str], edits: list[FileEdit], err: FileEditError) -> str:
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
        text = apply_file_edits(site_files, edits).files.get(err.path)
        if text is not None and err.offset is not None:
            lines += [
                f"The problem is at {line_col(text, err.offset)} of the edited file:",
                _quote(excerpt(text, err.offset)),
            ]
    return "\n".join(lines)


def _reason(err: FileEditError) -> str:
    """The error without its `edit N (path): ` prefix, which the report already says."""
    message = str(err)
    return message.split("): ", 1)[1] if message.startswith("edit ") and "): " in message else message


# Quote marks around project text, most usual first. The first pair the text doesn't contain is used, so the
# text can't end its own quote (it's copied exactly, so it can't be escaped).
QUOTES = (("⟦", "⟧"), ("⟪", "⟫"), ("⦃", "⦄"))


def _quote(text: str) -> str:
    """Quotes file text so its edges are visible: the model must copy it exactly, spaces included."""
    left, right = next(((a, b) for a, b in QUOTES if a not in text and b not in text), QUOTES[-1])
    return left + text + right
