from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status

from app.api.dependencies import CurrentUserDep, PublishServiceDep, publish_limit, publish_slot
from app.schemas.common import ApiResponse, ErrorResponse
from app.schemas.publish import (
    DeployRequest,
    DeployStarted,
    PublishRequest,
    PublishResult,
    PublishStatus,
    PublishTargetInfo,
    RepoName,
)

router = APIRouter(prefix="/publish", tags=["publish"])

ERRORS: dict[int | str, dict[str, Any]] = {
    code: {"model": ErrorResponse}
    for code in (
        status.HTTP_401_UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN,
        status.HTTP_404_NOT_FOUND,
        status.HTTP_409_CONFLICT,
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        status.HTTP_429_TOO_MANY_REQUESTS,
        status.HTTP_502_BAD_GATEWAY,
        status.HTTP_503_SERVICE_UNAVAILABLE,
    )
}


@router.get("/targets", summary="Where a site can be published")
async def list_targets(publisher: PublishServiceDep) -> ApiResponse[list[PublishTargetInfo]]:
    """The hosting platforms, with the Actions secrets each one's deploy workflow reads (what the user adds on
    GitHub if they don't paste a token in the chat) and where to make a token."""
    return ApiResponse(data=publisher.targets())


@router.post(
    "/{project_id}",
    summary="Publish a site",
    responses=ERRORS,
    dependencies=[Depends(publish_slot), Depends(publish_limit)],
)
async def publish_site(
    project_id: UUID, body: PublishRequest, user: CurrentUserDep, publisher: PublishServiceDep
) -> ApiResponse[PublishResult]:
    """Commits the site (built from the template catalog, the content and the AI-edited files) and a deploy
    workflow for `target` to the site repo on the user's GitHub, creating it on first publish, then starts the
    deploy (also when nothing changed). Pushes never deploy: the workflow only runs when makable starts it. With
    `credentials` (a platform token pasted in the chat), the token is checked, the platform project is created and
    the token is saved as an Actions secret; without it, `missingSecrets` lists what to add on GitHub and the deploy
    isn't started. `deployStartedAt` goes to the status check as `since`.

    409 `publish_changed_on_github` when files makable manages were changed on GitHub: send `details.headSha` back
    as `confirmOverwrite` to overwrite them."""
    return ApiResponse(data=await publisher.publish(user, project_id, body))


@router.post(
    "/{project_id}/deploy",
    summary="Run a published site's deploy again",
    responses=ERRORS,
    dependencies=[Depends(publish_slot), Depends(publish_limit)],
)
async def deploy_site(
    project_id: UUID, body: DeployRequest, user: CurrentUserDep, publisher: PublishServiceDep
) -> ApiResponse[DeployStarted]:
    """Starts the deploy workflow, e.g. after the user added the secrets on GitHub (409 `publish_secrets_missing`
    with `details.missing` if they're not all there), or with a token pasted in the chat (`credentials`)."""
    return ApiResponse(data=await publisher.deploy(user, project_id, body))


@router.get(
    "/{project_id}/status",
    summary="A published site's latest deploy",
    responses=ERRORS,
    dependencies=[Depends(publish_slot)],
)
async def get_status(
    project_id: UUID,
    repo_name: Annotated[RepoName, Query(alias="repoName")],
    user: CurrentUserDep,
    publisher: PublishServiceDep,
    since: Annotated[str | None, Query(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")] = None,
) -> ApiResponse[PublishStatus]:
    """The platform, the site's address and the deploy workflow's latest run (None before the first one). With
    `since` (a deploy's `startedAt` / `deployStartedAt`), only a run started then or later."""
    return ApiResponse(data=await publisher.status(user, project_id, repo_name, since))
