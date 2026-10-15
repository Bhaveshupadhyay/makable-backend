"""Reads the model's reply: the JSON in it, as edits, an escalation or a request for more code, and a short
description of what was wrong with it."""

import json
import re

from pydantic import TypeAdapter, ValidationError

from app.schemas.ai_edit import FileEdit, ModelAnswer, ModelEdits, ModelEscalation, ModelNeedsContext

type Answer = ModelEdits | ModelEscalation | ModelNeedsContext

_ANSWER: TypeAdapter[Answer] = TypeAdapter(ModelAnswer)
# A line as the prompt shows it: its number, "| ", then the text.
_NUMBERED = re.compile(r"^ *\d+\| ?")


def parse_answer(reply: str) -> Answer:
    """The model's answer.

    Raises:
        ValueError: The reply has no JSON object, or it isn't a valid answer (`ValidationError` and
            `JSONDecodeError` are `ValueError`s).
    """
    return _ANSWER.validate_python(extract_json(reply))


def without_line_numbers(edits: list[FileEdit]) -> list[FileEdit]:
    """The edits with the prompt's line numbers taken out of their search text, when every line of it has one: models
    sometimes copy them from the excerpts they were shown."""
    fixed = []
    for edit in edits:
        lines = edit.search.split("\n")
        if edit.search and all(_NUMBERED.match(line) for line in lines if line.strip()):
            search = "\n".join(_NUMBERED.sub("", line, count=1) for line in lines)
            replace_lines = edit.replace.split("\n")
            if all(_NUMBERED.match(line) for line in replace_lines if line.strip()):
                replace = "\n".join(_NUMBERED.sub("", line, count=1) for line in replace_lines)
            else:
                replace = edit.replace
            edit = edit.model_copy(update={"search": search, "replace": replace})
        fixed.append(edit)
    return fixed


def extract_json(text: str) -> object:
    """Pulls the JSON object out of a reply that may be wrapped in fences or prose.

    Raises:
        ValueError: The reply has no JSON object.
    """
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("The model did not return JSON")
    return json.loads(text[start : end + 1])


def describe_problem(err: Exception) -> str:
    if isinstance(err, ValidationError):
        return "; ".join(f"{'.'.join(str(p) for p in e['loc']) or 'the answer'}: {e['msg']}" for e in err.errors()[:5])
    return str(err)
