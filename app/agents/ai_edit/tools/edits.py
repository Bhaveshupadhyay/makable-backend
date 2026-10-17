"""Dry-runs the model's edits on the site's files at the request's base commit, and checks the result still parses.
The same edits are applied again for the commit, so a change that passed here is the one committed."""

from app.constants.ai_edit import FORBIDDEN_PATH
from app.schemas.ai_edit import FileEdit
from app.utils.file_edits import AppliedEdits, FileEditError, apply_file_edits
from app.utils.syntax_check import locate_syntax_error, locate_unbalanced_brace, script_language


def check_edits(files: dict[str, str], edits: list[FileEdit]) -> AppliedEdits:
    """Dry-runs the edits on the site's files and checks the result still parses: scripts must have no syntax
    errors, and CSS braces must balance. Edits that change nothing are rejected too, so the user is never told
    about a change that wasn't made.

    Raises:
        FileEditError: An edit can't be applied, or it breaks a file. The message is written for the model.
    """
    forbidden = next((i for i, e in enumerate(edits, start=1) if FORBIDDEN_PATH.search(e.path)), None)
    if forbidden:
        path = edits[forbidden - 1].path
        raise FileEditError(f"{path} can't be edited", kind="forbidden", edit=forbidden, path=path)
    applied = apply_file_edits(files, edits)
    if not applied.changed:
        raise FileEditError(
            "the edits don't change any file; make the requested change, or escalate if you can't", kind="no_change"
        )
    same = next((i for i, e in enumerate(edits, start=1) if e.search == e.replace), None)
    if same:
        path = edits[same - 1].path
        raise FileEditError(
            f"edit {same} ({path}): its search and replace are the same, so it changes nothing",
            kind="no_change",
            edit=same,
            path=path,
        )
    for path in applied.changed:
        found = _problem_in(path, applied.files[path])
        if found:
            problem, offset = found
            raise FileEditError(f"{path}: {problem}", kind="broken_file", path=path, offset=offset)
    return applied


def _problem_in(path: str, code: str) -> tuple[str, int | None] | None:
    """What's wrong with an edited file, and where (a character offset) when that's known."""
    language = script_language(path)
    if language is not None:
        return locate_syntax_error(code, language)
    if path.endswith(".css"):
        return locate_unbalanced_brace(code)
    return None
