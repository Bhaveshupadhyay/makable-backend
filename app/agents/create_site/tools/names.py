"""The site's name: one to suggest from the user's prompt (no model call: it's only a suggestion), and what the model
made of the user's answer to the name question."""

import re
from typing import Annotated

from pydantic import Field, StringConstraints, TypeAdapter

from app.agents.ai_edit.tools.answer import extract_json
from app.constants.ai_create import MAX_NAME_SUGGESTIONS
from app.constants.ai_edit import MAX_QUESTION
from app.constants.site import SITE_NAME_MAX
from app.schemas.common import CamelModel

# Words that describe the request rather than name the site.
_FILLER = frozenset(
    [
        "a",
        "an",
        "the",
        "my",
        "our",
        "your",
        "for",
        "of",
        "and",
        "with",
        "in",
        "on",
        "to",
        "at",
        "by",
        "from",
        "make",
        "create",
        "build",
        "design",
        "generate",
        "need",
        "want",
        "i",
        "we",
        "please",
        "new",
        "simple",
        "modern",
        "beautiful",
        "nice",
        "website",
        "web",
        "site",
        "page",
        "landing",
        "homepage",
        "home",
        "one",
        "that",
        "this",
        "called",
        "named",
        "me",
        "us",
        "can",
        "you",
        "some",
    ]
)
MAX_SUGGESTION_WORDS = 4
# Well under what a question's option (`MAX_OPTION`) and a site's name (`SITE_NAME_MAX`) can be.
MAX_SUGGESTION_CHARS = 40


def suggest_name(prompt: str) -> str:
    """A quoted name in the prompt if there is one, else its first few meaningful words, in title case; at most
    `MAX_SUGGESTION_CHARS` long."""
    quoted = re.search(r"\"([^\"]{2,40})\"|“([^”]{2,40})”|(?<!\w)'([^']{2,40})'(?!\w)", prompt)
    if quoted:
        return next(g for g in quoted.groups() if g).strip()
    words = [w for w in re.findall(r"[A-Za-z0-9][A-Za-z0-9&'-]*", prompt) if w.lower() not in _FILLER]
    name = ""
    for word in words[:MAX_SUGGESTION_WORDS]:
        if len(name) + len(word) + 1 > MAX_SUGGESTION_CHARS:
            break
        name = f"{name} {word}".strip()
    if not name and words:
        name = words[0][:MAX_SUGGESTION_CHARS]
    return name.title() if name else "My site"


def same_name(a: str, b: str) -> bool:
    """Whether two names are the same, ignoring case and spacing (a clicked option, or a name the user typed)."""
    return " ".join(a.split()).casefold() == " ".join(b.split()).casefold()


def mentions(text: str, name: str) -> bool:
    """Whether the user's text has the name in it, ignoring case and spacing."""
    return " ".join(name.split()).casefold() in " ".join(text.split()).casefold()


class NameGiven(CamelModel):
    """The user's answer names the site (maybe in a sentence: "call it Sunrise", or picking a suggestion)."""

    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=SITE_NAME_MAX)]


class NameChat(CamelModel):
    """The user's answer isn't a name (a greeting, a question, a request for ideas): what to say back, which asks for
    the name again, and names to suggest."""

    reply: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_QUESTION)]
    suggestions: Annotated[
        list[Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_SUGGESTION_CHARS)]],
        Field(max_length=MAX_NAME_SUGGESTIONS),
    ] = Field(default_factory=list)


_NAME_REPLY: TypeAdapter[NameGiven | NameChat] = TypeAdapter(
    Annotated[NameGiven | NameChat, Field(union_mode="left_to_right")]
)


def parse_name_reply(reply: str) -> NameGiven | NameChat:
    """What the model made of the user's answer to the name question.

    Raises:
        ValueError: No JSON, or neither a name nor a reply (`ValidationError` is a `ValueError`).
    """
    return _NAME_REPLY.validate_python(extract_json(reply))
