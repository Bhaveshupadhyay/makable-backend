"""The "Edit with AI" contract with the SPA (`@makable/shared` `ai-edit.ts`).

The browser sends the instruction, what's selected, and the commit its copy of the site is at; the server reads the
code itself from the site's repo at that commit and commits the change. The selected element is described by the
preview page, so everything here is untrusted: it's length-capped and only ever goes into the prompt as data.
"""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import AfterValidator, Field, StringConstraints

from app.constants.ai_edit import (
    MAX_ANSWER,
    MAX_EDITS,
    MAX_ELEMENT_HTML,
    MAX_INSTRUCTION,
    MAX_OPTION,
    MAX_OPTIONS,
    MAX_PREVIEW_ERROR,
    MAX_QUESTION,
    MAX_SCREENSHOT_CHARS,
    MAX_TARGET_LABEL,
    SAFE_REPO_PATH,
    SCREENSHOT,
)
from app.schemas.ai_key import ModelChoice
from app.schemas.common import CamelModel


def _safe_repo_path(value: str) -> str:
    if not SAFE_REPO_PATH.fullmatch(value):
        raise ValueError("must be a relative path inside the project")
    return value


def _screenshot(value: str) -> str:
    if not SCREENSHOT.fullmatch(value):
        raise ValueError("must be a base64 data URL of a PNG, JPEG or WebP image")
    return value


RepoPath = Annotated[str, AfterValidator(_safe_repo_path)]
Sentence = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
CommitSha = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{40}$")]
Screenshot = Annotated[str, Field(max_length=MAX_SCREENSHOT_CHARS), AfterValidator(_screenshot)]


class AiEditSection(CamelModel):
    tag: Annotated[str, Field(max_length=32)]
    id: Annotated[str, Field(max_length=100)] | None
    heading: Annotated[str, Field(max_length=120)] | None


class AiEditSource(CamelModel):
    """Where an element is in the code: the preview tags elements with `data-src="<path>:<line>:<column>"`."""

    path: RepoPath
    line: Annotated[int, Field(ge=1, le=1_000_000)]
    column: Annotated[int, Field(ge=0, le=100_000)]


class AiEditTarget(CamelModel):
    """The element the user clicked in the preview."""

    tag: Annotated[str, Field(max_length=32)]
    # Visible text, whitespace-collapsed.
    text: Annotated[str, Field(max_length=300)]
    # Where it's written in the code; None when the preview couldn't tell (e.g. an element made by a script).
    source: AiEditSource | None
    # The enclosing landmark (section, header, footer, nav, aside).
    section: AiEditSection | None
    # CSS-like path from <body>, e.g. `main > section#skills > ul > li:nth-of-type(3)`.
    selector: Annotated[str, Field(max_length=500)]
    # The element's outerHTML, truncated.
    html: Annotated[str, Field(max_length=MAX_ELEMENT_HTML)]


Instruction = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_INSTRUCTION)]


class AiEditTurn(CamelModel):
    """An earlier request on this site and how it went, so follow-ups like "make it bigger" work. They come from the
    site's thread and go into the prompt as data: the instructions in them are the user's own, earlier, untrusted
    text."""

    instruction: Instruction
    # A short label for what was selected, e.g. `Hero > <a> "Contact"`.
    target: Annotated[str, Field(max_length=MAX_TARGET_LABEL)] | None
    # What happened: the summary of the change, or why nothing changed.
    reply: str


class AiEditRequest(CamelModel):
    # The site (the SPA's `projectId`).
    project_id: UUID
    # The commit the browser's copy of the site is at (it saves its own edits first). The code is read there.
    base_commit: CommitSha
    instruction: Instruction
    # None when nothing was selected: the request is about the whole site.
    target: AiEditTarget | None
    # A picture of the selected element, for models that read images.
    screenshot: Screenshot | None = None
    # The model the user picked in the chat's menu; left out for makable's own.
    model: ModelChoice | None = None


class FileEdit(CamelModel):
    """A search/replace edit. The search text must be found exactly once in the file. An empty search creates the
    file (it mustn't exist yet) with `replace` as its text."""

    path: RepoPath
    search: Annotated[str, Field(max_length=20_000)]
    replace: Annotated[str, Field(max_length=60_000)]


class ModelEdits(CamelModel):
    """The model's answer when it can make the change."""

    summary: Sentence
    edits: Annotated[list[FileEdit], Field(max_length=MAX_EDITS)]


class ModelEscalation(CamelModel):
    """The model's answer when the change needs a deeper (Tier 2) edit."""

    escalate: Sentence


class ContextRequest(CamelModel):
    path: Annotated[str, Field(max_length=300)]
    # First and last line (1-based); left out for the whole file.
    lines: Annotated[list[int], Field(min_length=2, max_length=2)] | None = None


class ModelNeedsContext(CamelModel):
    """The model's answer when it needs to see more of the code first."""

    need_context: ContextRequest


class Question(CamelModel):
    """A question for the user, with choices to click (colours as CSS colours) and/or free text."""

    question: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_QUESTION)]
    options: Annotated[
        list[Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_OPTION)]],
        Field(max_length=MAX_OPTIONS),
    ] = Field(default_factory=list)
    allow_free_text: bool = True


class ModelQuestion(CamelModel):
    """The model's answer when only the user can say what they want."""

    ask: Question


ModelAnswer = Annotated[
    ModelEdits | ModelEscalation | ModelNeedsContext | ModelQuestion, Field(union_mode="left_to_right")
]


class AiEditDebug(CamelModel):
    """Only sent when `AI_DEBUG` is on: what the model was given last, for the network tab."""

    model_input: str
    attempts: int
    model: str


class AiEditApplied(CamelModel):
    """The change is committed to the site's repo. `files` are the files it changed, as they are now (None: deleted),
    which the SPA puts in its copy of the site at `commit_sha`."""

    status: Literal["applied"] = "applied"
    run_id: UUID
    summary: str
    commit_sha: str
    files: dict[str, str | None]
    debug: AiEditDebug | None = None


class AiEditEscalated(CamelModel):
    """The change needs a deeper edit than Tier 1 can make. Nothing was changed."""

    status: Literal["escalated"] = "escalated"
    run_id: UUID
    reason: str
    debug: AiEditDebug | None = None


class AiEditNeedsInput(CamelModel):
    """The run is paused on a question for the user. It's saved: answering continues it (`/answer`), even from another
    tab or after the page was closed."""

    status: Literal["needs_input"] = "needs_input"
    run_id: UUID
    question: Question
    debug: AiEditDebug | None = None


class AiEditAwaitingResult(CamelModel):
    """The edits passed the checks and wait for the preview: the SPA puts `files` (the files they change, as they'd be;
    None: deleted) in its preview and reports how it went (`/result`). Nothing is committed until it says it works."""

    status: Literal["awaiting_result"] = "awaiting_result"
    run_id: UUID
    summary: str
    files: dict[str, str | None]
    debug: AiEditDebug | None = None


AiEditResult = Annotated[
    AiEditApplied | AiEditEscalated | AiEditNeedsInput | AiEditAwaitingResult, Field(discriminator="status")
]


class PreviewError(CamelModel):
    """What the preview reported: its message, and where in the code when it knows."""

    message: Annotated[str, Field(max_length=MAX_PREVIEW_ERROR * 4)]
    path: RepoPath | None = None
    line: Annotated[int, Field(ge=1, le=1_000_000)] | None = None


class AiEditPreviewReport(CamelModel):
    """How the preview went with a run's edits: `ok`, or the `error` it showed."""

    run_id: UUID
    ok: bool
    error: PreviewError | None = None


class AiEditAnswer(CamelModel):
    """The user's answer to a paused run's question. `base_commit` is where the site is now (the SPA saves first):
    the code is read again there."""

    run_id: UUID
    answer: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_ANSWER)]
    base_commit: CommitSha
    screenshot: Screenshot | None = None
