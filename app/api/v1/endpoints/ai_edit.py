import logging
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Request, status

from app.api.dependencies import AiEditServiceDep, CurrentUserDep, ai_edit_slot
from app.api.disconnect import cancel_on_disconnect
from app.schemas.ai_edit import AiEditAnswer, AiEditRequest, AiEditResult
from app.schemas.ai_thread import AiThreadRead
from app.schemas.common import ApiResponse, ErrorResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ai", tags=["ai"])

ERRORS: dict[int | str, dict[str, Any]] = {
    status.HTTP_401_UNAUTHORIZED: {"model": ErrorResponse},
    status.HTTP_404_NOT_FOUND: {"model": ErrorResponse},
    status.HTTP_409_CONFLICT: {"model": ErrorResponse},
    status.HTTP_422_UNPROCESSABLE_CONTENT: {"model": ErrorResponse},
    status.HTTP_429_TOO_MANY_REQUESTS: {"model": ErrorResponse},
    status.HTTP_502_BAD_GATEWAY: {"model": ErrorResponse},
    status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ErrorResponse},
}


@router.post(
    "/edit",
    summary="Edit the site with AI",
    responses=ERRORS,
    response_model_exclude_none=True,
    dependencies=[Depends(ai_edit_slot)],
)
async def edit(
    body: AiEditRequest, request: Request, user: CurrentUserDep, ai_edit: AiEditServiceDep
) -> ApiResponse[AiEditResult]:
    """The agent reads the site's code at `baseCommit` from its repo, makes the change and commits it:
    `{status: "applied", commitSha, files}` (the files it changed, as they are now), or `{status: "escalated", reason}`
    when the change needs a deeper edit. Closing the request (Stop) cancels it, unless the commit had started (then
    it's in the chat). 409 `ai_run_in_progress` while the site has a request in progress, 409 `site_conflict` when
    the site isn't at `baseCommit` on GitHub any more."""
    result = await cancel_on_disconnect(request, ai_edit.edit(user, body))
    logger.info("AI edit", extra={"user_id": str(user.id), "status": result.status})
    return ApiResponse(data=result)


@router.post(
    "/edit/{project_id}/answer",
    summary="Answer the AI's question",
    responses=ERRORS,
    response_model_exclude_none=True,
    dependencies=[Depends(ai_edit_slot)],
)
async def answer(
    project_id: UUID, body: AiEditAnswer, request: Request, user: CurrentUserDep, ai_edit: AiEditServiceDep
) -> ApiResponse[AiEditResult]:
    """Continues the run that asked (`runId`) with the user's answer, on the site at `baseCommit` (the SPA saves
    first). Answers like `/edit`. 409 `ai_run_superseded` when no run is waiting for it any more."""
    result = await cancel_on_disconnect(request, ai_edit.answer(user, project_id, body))
    logger.info("AI edit answered", extra={"user_id": str(user.id), "status": result.status})
    return ApiResponse(data=result)


@router.get("/threads/{project_id}", summary="The site's AI chat", responses=ERRORS)
async def get_thread(project_id: UUID, user: CurrentUserDep, ai_edit: AiEditServiceDep) -> ApiResponse[AiThreadRead]:
    """The site's finished requests, oldest first (empty for a site with no AI requests yet), and `status:
    "needs_input"` with the `run` and its question when a run is waiting for the user (after a closed tab, too)."""
    return ApiResponse(data=await ai_edit.thread(user, project_id))


@router.delete(
    "/threads/{project_id}",
    summary="Forget the site (Start over)",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=ERRORS,
)
async def delete_thread(project_id: UUID, user: CurrentUserDep, ai_edit: AiEditServiceDep) -> None:
    """Deletes the site's chat, its stored requests and makable's record of it (Start over). Its repo on GitHub
    isn't touched."""
    await ai_edit.start_over(user, project_id)


@router.delete(
    "/threads/{project_id}/run",
    summary="Dismiss the AI's question",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=ERRORS,
)
async def dismiss(project_id: UUID, user: CurrentUserDep, ai_edit: AiEditServiceDep) -> None:
    """Ends the run that's waiting for the user's answer, without a change. Nothing waiting is fine."""
    await ai_edit.dismiss(user, project_id)
