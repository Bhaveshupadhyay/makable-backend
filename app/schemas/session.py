"""Pieces of a builder session, mirroring the SPA's `@makable/shared` `session.ts` (same caps). They come from the
browser, so they're untrusted and validated before anything is written to the user's repo."""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import AfterValidator, Field, RootModel

from app.constants.workspace import CHUNK_MAX_MESSAGES, MAX_MESSAGE_CHARS, MAX_STORED_TURNS
from app.schemas.ai_edit import AiEditTurn
from app.schemas.common import CamelModel


def _iso_datetime(value: str) -> str:
    # Kept as the string the SPA wrote, so it round-trips unchanged; only checked here.
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError("must be an ISO 8601 date and time") from None
    if parsed.tzinfo is None:
        raise ValueError("must include a time zone")
    return value


IsoDateTime = Annotated[str, AfterValidator(_iso_datetime)]
Id = Annotated[str, Field(min_length=1, max_length=100)]


class SessionMessage(CamelModel):
    id: Id
    role: Literal["user", "assistant"]
    text: Annotated[str, Field(max_length=MAX_MESSAGE_CHARS)]
    widget: Literal["template-picker", "connect-github"] | None = None
    replies: Annotated[list[Annotated[str, Field(max_length=200)]], Field(max_length=20)] | None = None


class SessionTurn(AiEditTurn):
    id: Id


class MessageChunk(CamelModel):
    """One `messages/NNNN.json` file: a run of the chat, in order."""

    messages: Annotated[list[SessionMessage], Field(min_length=1, max_length=CHUNK_MAX_MESSAGES)]


class AiHistoryFile(RootModel[Annotated[list[SessionTurn], Field(max_length=MAX_STORED_TURNS)]]):
    """`ai-history/<template>.json`: a template's finished AI requests, oldest first."""
