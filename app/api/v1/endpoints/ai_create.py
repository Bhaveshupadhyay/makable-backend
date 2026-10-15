import json
import logging
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, status
from fastapi.responses import StreamingResponse

from app.api.dependencies import CurrentUserDep, SiteCreateServiceDep, ai_create_slot
from app.schemas.ai_create import AiCreateAnswer, AiCreateRequest
from app.schemas.common import ErrorResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ai/create", tags=["ai"], dependencies=[Depends(ai_create_slot)])

ERRORS: dict[int | str, dict[str, Any]] = {
    code: {"model": ErrorResponse}
    for code in (
        status.HTTP_401_UNAUTHORIZED,
        status.HTTP_404_NOT_FOUND,
        status.HTTP_409_CONFLICT,
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        status.HTTP_503_SERVICE_UNAVAILABLE,
    )
}

# A stream of JSON lines: Nginx mustn't buffer it, and nothing may cache it.
STREAM_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
NDJSON = "application/x-ndjson"


def _lines(events: AsyncIterator[dict[str, Any]]) -> AsyncIterator[str]:
    async def lines() -> AsyncIterator[str]:
        async for event in events:
            yield json.dumps(event, separators=(",", ":")) + "\n"

    return lines()


@router.post("", summary="Create a site from a prompt", responses=ERRORS)
async def create(body: AiCreateRequest, user: CurrentUserDep, creator: SiteCreateServiceDep) -> StreamingResponse:
    """Builds the site from the prompt: a stream of JSON lines (`application/x-ndjson`). `{type: "progress", stage,
    message, path?, done?, total?}` as it goes, `{type: "ping"}` while the model writes, and at the end `{type:
    "result", data}` (`{status: "needs_input", runId, question}` for the site's name: answer with `/answer`; or
    `{status: "created", runId, summary, commitSha, site}`) or `{type: "error", error: {code, message, details}}`.
    Errors before the stream starts (another user's site, a request in progress) are the usual envelopes. Closing the
    request stops the creation."""
    events = await creator.create(user, body)
    logger.info("site creation started", extra={"user_id": str(user.id), "project_id": str(body.project_id)})
    return StreamingResponse(_lines(events), media_type=NDJSON, headers=STREAM_HEADERS)


@router.post("/{project_id}/answer", summary="Answer the creation's question", responses=ERRORS)
async def answer(
    project_id: UUID, body: AiCreateAnswer, user: CurrentUserDep, creator: SiteCreateServiceDep
) -> StreamingResponse:
    """Continues the creation that asked for the site's name, streamed like `/ai/create`. 409 `ai_run_superseded` when
    no creation is waiting for it."""
    events = await creator.answer(user, project_id, body)
    return StreamingResponse(_lines(events), media_type=NDJSON, headers=STREAM_HEADERS)
