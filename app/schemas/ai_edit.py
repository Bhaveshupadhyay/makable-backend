"""The "Edit with AI" contract with the SPA (`@makable/shared` `ai-edit.ts` and `ai-edit-files.ts`).

Everything in a request comes from the browser, and the selected element is described by the preview page
itself, so all of it is untrusted: it's length-capped here and only ever goes into the prompt as data.
"""

from typing import Annotated, Literal

from pydantic import AfterValidator, Field, StringConstraints

from app.constants.ai_edit import (
    MAX_EDITS,
    MAX_ELEMENT_HTML,
    MAX_FILE_CHARS,
    MAX_FILE_TREE,
    MAX_FILES,
    MAX_HISTORY,
    MAX_HISTORY_REPLY,
    MAX_INSTRUCTION,
    MAX_TARGET_LABEL,
    SAFE_REPO_PATH,
)
from app.schemas.common import CamelModel
from app.schemas.portfolio import TemplateId


def _safe_repo_path(value: str) -> str:
    if not SAFE_REPO_PATH.fullmatch(value):
        raise ValueError("must be a relative path inside the project")
    return value


RepoPath = Annotated[str, AfterValidator(_safe_repo_path)]
Sentence = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]


class AiEditSection(CamelModel):
    tag: Annotated[str, Field(max_length=32)]
    id: Annotated[str, Field(max_length=100)] | None
    heading: Annotated[str, Field(max_length=120)] | None


class AiEditTarget(CamelModel):
    """The element the user clicked in the preview."""

    tag: Annotated[str, Field(max_length=32)]
    # Visible text, whitespace-collapsed.
    text: Annotated[str, Field(max_length=300)]
    # The nearest `data-content` path (e.g. `skills.2`), if the element shows content.
    content_path: Annotated[str, Field(max_length=100)] | None
    # The enclosing landmark (section, header, footer, nav, aside).
    section: AiEditSection | None
    # CSS-like path from <body>, e.g. `main > section#skills > ul > li:nth-of-type(3)`.
    selector: Annotated[str, Field(max_length=500)]
    # The element's outerHTML, truncated.
    html: Annotated[str, Field(max_length=MAX_ELEMENT_HTML)]


class AiEditTemplate(CamelModel):
    id: TemplateId
    name: Annotated[str, Field(max_length=100)]
    kind: Literal["react", "static"]
    version: Annotated[int, Field(gt=0)]
    # The generated content file; all copy lives there.
    content_path: RepoPath


class AiEditFile(CamelModel):
    path: RepoPath
    content: Annotated[str, Field(max_length=MAX_FILE_CHARS)]
    # Why the SPA picked this file, e.g. `mentions "skills"`. Helps the model.
    reason: Annotated[str, Field(max_length=200)]


Instruction = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_INSTRUCTION)]


class AiEditTurn(CamelModel):
    """An earlier request in this project and how it went, so follow-ups like "make it bigger" work. The
    browser keeps these, so they're untrusted: they go into the prompt as data, like everything else."""

    instruction: Instruction
    # A short label for what was selected, e.g. `Hero > <a> "Contact"`.
    target: Annotated[str, Field(max_length=MAX_TARGET_LABEL)] | None
    # What happened: the summary of the change, or why nothing changed.
    reply: Annotated[str, Field(max_length=MAX_HISTORY_REPLY)]


class AiEditRequest(CamelModel):
    instruction: Instruction
    # None when nothing was selected: the request is about the whole page.
    target: AiEditTarget | None
    template: AiEditTemplate
    # Every text file in the project, so the model knows the layout.
    file_tree: Annotated[list[RepoPath], Field(max_length=MAX_FILE_TREE)]
    # The files most likely to change, with their contents.
    files: Annotated[list[AiEditFile], Field(max_length=MAX_FILES)]
    # Earlier requests, oldest first.
    history: Annotated[list[AiEditTurn], Field(max_length=MAX_HISTORY, default_factory=list)]


class FileEdit(CamelModel):
    """A search/replace edit. The search text must be found exactly once in the file."""

    path: RepoPath
    search: Annotated[str, Field(min_length=1, max_length=20_000)]
    replace: Annotated[str, Field(max_length=40_000)]


class ModelEdits(CamelModel):
    """The model's answer when it can make the change."""

    summary: Sentence
    edits: Annotated[list[FileEdit], Field(max_length=MAX_EDITS)]


class ModelEscalation(CamelModel):
    """The model's answer when the change needs a deeper (Tier 2) edit."""

    escalate: Sentence


ModelAnswer = Annotated[ModelEdits | ModelEscalation, Field(union_mode="left_to_right")]


class AiEditDebug(CamelModel):
    """Only sent when `AI_DEBUG` is on: what the model was given, for the network tab."""

    model_input: str
    attempts: int
    model: str


class AiEditTier1(CamelModel):
    """Edits the SPA applies with `applyFileEdits`."""

    tier: Literal[1] = 1
    summary: str
    edits: list[FileEdit]
    debug: AiEditDebug | None = None


class AiEditTier2(CamelModel):
    """The change needs a deeper edit than Tier 1 can make."""

    tier: Literal[2] = 2
    reason: str
    debug: AiEditDebug | None = None


AiEditResult = Annotated[AiEditTier1 | AiEditTier2, Field(discriminator="tier")]
