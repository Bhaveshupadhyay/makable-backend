"""The published template catalog (`@makable/shared` `template-catalog.ts`), as far as publishing needs it."""

from dataclasses import dataclass
from typing import Literal

from pydantic import Field

from app.schemas.ai_edit import RepoPath
from app.schemas.common import CamelModel
from app.schemas.portfolio import TemplateId

TemplateKind = Literal["react", "static"]


class TemplateEntry(CamelModel):
    id: TemplateId
    kind: TemplateKind
    version: int = Field(gt=0)
    # React templates that share one codebase pick their look by theme; it becomes the content's `template`.
    theme: str | None = None
    content_path: RepoPath
    files_url: str


@dataclass(frozen=True)
class TemplateFiles:
    """A template's files: text by repo path, and binaries (images) decoded from files.json's base64."""

    text: dict[str, str]
    binary: dict[str, bytes]
