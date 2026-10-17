"""The questions about the site asked first: what the model replies (the next question, or that it knows enough, with
names to suggest for the site), and the user's answers as the name, the plan and the files are told them."""

from typing import Annotated, Literal

from pydantic import Field, StringConstraints, TypeAdapter

from app.agents.ai_edit.tools.answer import extract_json
from app.agents.create_site.tools.names import MAX_SUGGESTION_CHARS
from app.agents.thread import QuestionAnswer
from app.constants.ai_create import MAX_NAME_SUGGESTIONS
from app.schemas.ai_edit import ModelQuestion
from app.schemas.common import CamelModel


class BriefReady(CamelModel):
    """The model knows enough to plan the site, and suggests names for it (the name question's options)."""

    ready: Literal[True]
    names: Annotated[
        list[Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_SUGGESTION_CHARS)]],
        Field(max_length=MAX_NAME_SUGGESTIONS),
    ] = Field(default_factory=list)


_BRIEF_REPLY: TypeAdapter[ModelQuestion | BriefReady] = TypeAdapter(
    Annotated[ModelQuestion | BriefReady, Field(union_mode="left_to_right")]
)


def parse_brief_reply(reply: str) -> ModelQuestion | BriefReady:
    """The model's next question about the site, or that it's ready to plan it.

    Raises:
        ValueError: No JSON, or neither a question nor ready (`ValidationError` is a `ValueError`).
    """
    return _BRIEF_REPLY.validate_python(extract_json(reply))


def answered(brief: list[QuestionAnswer]) -> int:
    """The questions the user answered (a message about one, "Chat about this", isn't an answer)."""
    return sum(not q.get("chat") for q in brief)


def brief_block(brief: list[QuestionAnswer]) -> str:
    """The user's answers, for the name's, the plan's and the files' prompts (empty without any)."""
    pairs = "\n".join(f"Q: {q['question']}\nA: {q['answer']}" for q in brief if not q.get("chat"))
    return f"<user_answers>\n{pairs}\n</user_answers>\n" if pairs else ""
