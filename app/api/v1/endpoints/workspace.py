from typing import Any
from uuid import UUID

from fastapi import APIRouter, status

from app.api.dependencies import CurrentUserDep, WorkspaceServiceDep
from app.schemas.common import ApiResponse, ErrorResponse
from app.schemas.workspace import WorkspaceSaved, WorkspaceSession, WorkspaceSessionWrite

router = APIRouter(prefix="/workspace", tags=["workspace"])

ERRORS: dict[int | str, dict[str, Any]] = {
    code: {"model": ErrorResponse}
    for code in (
        status.HTTP_401_UNAUTHORIZED,
        status.HTTP_404_NOT_FOUND,
        status.HTTP_409_CONFLICT,
        status.HTTP_413_CONTENT_TOO_LARGE,
        status.HTTP_502_BAD_GATEWAY,
    )
}


@router.get("/sessions/latest", summary="The newest saved session", responses=ERRORS)
async def get_latest_session(user: CurrentUserDep, workspace: WorkspaceServiceDep) -> ApiResponse[WorkspaceSession]:
    """The most recently saved session from the user's private `makable-workspace` repo, to restore on a new
    device. 404 when nothing is saved yet."""
    return ApiResponse(data=await workspace.get_latest_session(user))


@router.get("/sessions/{project_id}", summary="A site's saved session", responses=ERRORS)
async def get_session(
    project_id: UUID, user: CurrentUserDep, workspace: WorkspaceServiceDep
) -> ApiResponse[WorkspaceSession]:
    """The session saved for one site, with the SHA a later save must send back."""
    return ApiResponse(data=await workspace.get_session(user, project_id))


@router.put("/sessions/{project_id}", summary="Save a site's session", responses=ERRORS)
async def save_session(
    project_id: UUID, body: WorkspaceSessionWrite, user: CurrentUserDep, workspace: WorkspaceServiceDep
) -> ApiResponse[WorkspaceSaved]:
    """Commits the session to the private workspace repo, creating the repo on first use. `baseSha` is the
    SHA of the version it replaces: if the saved file changed since (another device), the answer is 409
    `workspace_conflict` with the current `sha` in `details`, and nothing is overwritten."""
    return ApiResponse(data=await workspace.save_session(user, project_id, body))
