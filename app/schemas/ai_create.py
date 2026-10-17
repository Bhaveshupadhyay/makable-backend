"""Creating a site from a prompt: the request, the answer to its name question, and what comes back at the end."""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import StringConstraints

from app.constants.ai_create import MAX_PROMPT
from app.constants.ai_edit import MAX_ANSWER
from app.schemas.ai_key import ModelChoice
from app.schemas.common import CamelModel
from app.schemas.site import SiteRead


class AiCreateRequest(CamelModel):
    # The site (the SPA's `projectId`). A site that has its repo already is made again over its files.
    project_id: UUID
    # What the user wants, in their words.
    prompt: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_PROMPT)]
    model: ModelChoice | None = None


class AiCreateAnswer(CamelModel):
    """The answer to the create agent's question (the site's name)."""

    run_id: UUID
    answer: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_ANSWER)]
    # A message about the question ("Chat about this"), not its answer: the AI replies and asks again.
    chat: bool = False


class AiCreated(CamelModel):
    """The site is created and committed to its repo; the SPA loads it from there."""

    status: Literal["created"] = "created"
    run_id: UUID
    summary: str
    commit_sha: str
    site: SiteRead
