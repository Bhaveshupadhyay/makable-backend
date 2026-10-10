from typing import Literal

from pydantic import BaseModel


class ChatMessage(BaseModel):
    """One turn of an OpenAI-compatible chat. No system role: the free models behind OmniRoute ignore it."""

    role: Literal["user", "assistant"]
    content: str
