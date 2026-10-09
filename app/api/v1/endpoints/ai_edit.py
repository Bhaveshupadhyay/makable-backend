import logging
from typing import Any

from fastapi import APIRouter, Request, status

from app.api.dependencies import AiEditServiceDep, CurrentUserDep
from app.api.disconnect import cancel_on_disconnect
from app.schemas.ai_edit import AiEditRequest, AiEditResult
from app.schemas.common import ApiResponse, ErrorResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ai", tags=["ai"])

ERRORS: dict[int | str, dict[str, Any]] = {
    status.HTTP_401_UNAUTHORIZED: {"model": ErrorResponse},
    status.HTTP_502_BAD_GATEWAY: {"model": ErrorResponse},
}


@router.post("/edit", summary="Edit the site with AI", responses=ERRORS, response_model_exclude_none=True)
async def edit(
    body: AiEditRequest, request: Request, user: CurrentUserDep, ai_edit: AiEditServiceDep
) -> ApiResponse[AiEditResult]:
    """Tier 1: search/replace edits on the files sent, checked on the server, or `{tier: 2, reason}` when the
    change needs a deeper edit. The SPA applies the edits itself. Closing the request (Stop) cancels the
    model call."""
    result = await cancel_on_disconnect(request, ai_edit.edit(body))
    logger.info("AI edit", extra={"user_id": str(user.id), "tier": result.tier})
    return ApiResponse(data=result)
