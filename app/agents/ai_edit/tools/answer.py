"""Reads the model's reply: the JSON in it, as edits or an escalation, and a short description of what was wrong
with it."""

import json

from pydantic import TypeAdapter, ValidationError

from app.schemas.ai_edit import ModelAnswer, ModelEdits, ModelEscalation

_ANSWER: TypeAdapter[ModelEdits | ModelEscalation] = TypeAdapter(ModelAnswer)


def parse_answer(reply: str) -> ModelEdits | ModelEscalation:
    """The model's answer.

    Raises:
        ValueError: The reply has no JSON object, or it isn't a valid answer (`ValidationError` and
            `JSONDecodeError` are `ValueError`s).
    """
    return _ANSWER.validate_python(extract_json(reply))


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
