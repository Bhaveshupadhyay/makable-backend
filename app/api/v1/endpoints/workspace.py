from typing import Any
from uuid import UUID

from fastapi import APIRouter, status

from app.api.dependencies import CurrentUserDep, WorkspaceServiceDep
from app.schemas.common import ApiResponse, ErrorResponse
from app.schemas.workspace import WorkspacePartRead, WorkspaceSave, WorkspaceSaved, WorkspaceStateRead

router = APIRouter(prefix="/workspace", tags=["workspace"])

ERRORS: dict[int | str, dict[str, Any]] = {
    code: {"model": ErrorResponse}
    for code in (
        status.HTTP_401_UNAUTHORIZED,
        status.HTTP_404_NOT_FOUND,
        status.HTTP_409_CONFLICT,
        status.HTTP_429_TOO_MANY_REQUESTS,
        status.HTTP_502_BAD_GATEWAY,
    )
}


@router.get("/sessions/latest", summary="The newest saved session", responses=ERRORS)
async def get_latest_session(user: CurrentUserDep, workspace: WorkspaceServiceDep) -> ApiResponse[WorkspaceStateRead]:
    """The state of the most recently saved session in the user's private `makable-workspace` repo, to restore on
    a new device. Its chat, AI history and files are read with the parts endpoint. 404 when nothing is saved."""
    return ApiResponse(data=await workspace.get_latest_state(user))


@router.get("/sessions/{project_id}", summary="A site's saved session", responses=ERRORS)
async def get_session(
    project_id: UUID, user: CurrentUserDep, workspace: WorkspaceServiceDep
) -> ApiResponse[WorkspaceStateRead]:
    """The state saved for one site, with the SHA a later save must send back."""
    return ApiResponse(data=await workspace.get_state(user, project_id))


@router.get("/sessions/{project_id}/parts/{path:path}", summary="One file of a saved session", responses=ERRORS)
async def get_session_part(
    project_id: UUID, path: str, user: CurrentUserDep, workspace: WorkspaceServiceDep
) -> ApiResponse[WorkspacePartRead]:
    """A message chunk (`messages/0001.json`), the AI history (`ai-history.json`) or an AI-edited file
    (`files/<template>/<path>`), as saved."""
    return ApiResponse(data=await workspace.get_part(user, project_id, path))


@router.put("/sessions/{project_id}", summary="Save a site's session", responses=ERRORS)
async def save_session(
    project_id: UUID, body: WorkspaceSave, user: CurrentUserDep, workspace: WorkspaceServiceDep
) -> ApiResponse[WorkspaceSaved]:
    """Commits the new state plus the files that changed (and removes the ones in `deletes`) as one commit,
    creating the private workspace repo on first use. `baseSha` is the SHA of the state it replaces: if the
    state changed since (another device), the answer is 409 `workspace_conflict` with the current `sha` in
    `details`, and nothing is written."""
    return ApiResponse(data=await workspace.save(user, project_id, body))
