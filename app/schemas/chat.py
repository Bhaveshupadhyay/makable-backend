from typing import Literal

from pydantic import BaseModel


class TextPart(BaseModel):
    type: Literal["text"] = "text"
    text: str


class ImageUrl(BaseModel):
    # A data URL (`data:image/jpeg;base64,...`): every OpenAI-compatible API that reads images takes one.
    url: str


class ImagePart(BaseModel):
    type: Literal["image_url"] = "image_url"
    image_url: ImageUrl


class ChatMessage(BaseModel):
    """One turn of an OpenAI-compatible chat. No system role: the free models behind OmniRoute ignore it. The content
    is text, or text and images for models that read images."""

    role: Literal["user", "assistant"]
    content: str | list[TextPart | ImagePart]

    @property
    def text(self) -> str:
        """The message's text, without its images."""
        if isinstance(self.content, str):
            return self.content
        return "\n".join(part.text for part in self.content if isinstance(part, TextPart))
