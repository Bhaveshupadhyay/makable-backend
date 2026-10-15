from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, status

from app.api.dependencies import CurrentUserDep, SiteServiceDep, site_save_limit, site_slot
from app.schemas.common import ApiResponse, ErrorResponse
from app.schemas.site import SiteFiles, SiteNew, SiteRead, SiteSave, SiteSaved, SiteUndo, SiteUndone

# Every site request waits on GitHub, so each holds one of the worker's slots.
router = APIRouter(prefix="/sites", tags=["sites"], dependencies=[Depends(site_slot)])

ERRORS: dict[int | str, dict[str, Any]] = {
    code: {"model": ErrorResponse}
    for code in (
        status.HTTP_401_UNAUTHORIZED,
        status.HTTP_404_NOT_FOUND,
        status.HTTP_409_CONFLICT,
        status.HTTP_413_CONTENT_TOO_LARGE,
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        status.HTTP_429_TOO_MANY_REQUESTS,
        status.HTTP_502_BAD_GATEWAY,
        status.HTTP_503_SERVICE_UNAVAILABLE,
        status.HTTP_504_GATEWAY_TIMEOUT,
    )
}


@router.get("", summary="The user's sites", responses=ERRORS)
async def list_sites(user: CurrentUserDep, sites: SiteServiceDep) -> ApiResponse[list[SiteRead]]:
    """The user's sites, the most recently used first (a new device opens the first)."""
    return ApiResponse(data=await sites.list_sites(user))


@router.post("/{project_id}", summary="Create a site's repo", status_code=status.HTTP_201_CREATED, responses=ERRORS)
async def create_site(
    project_id: UUID, body: SiteNew, user: CurrentUserDep, sites: SiteServiceDep
) -> ApiResponse[SiteFiles]:
    """Creates the site's private repo on the user's GitHub, named after the site, with makable's starter files, and
    returns them. 409 `site_repo_taken` (`details.repoName`) when the user has another repo with that name."""
    return ApiResponse(data=await sites.create(user, project_id, body))


@router.get("/{project_id}", summary="A site's files", responses=ERRORS)
async def get_site(project_id: UUID, user: CurrentUserDep, sites: SiteServiceDep) -> ApiResponse[SiteFiles]:
    """The site's files on GitHub now, and the commit they're at (what the next save builds on)."""
    return ApiResponse(data=await sites.load(user, project_id))


@router.put(
    "/{project_id}/files",
    summary="Save manual edits",
    responses=ERRORS,
    dependencies=[Depends(site_save_limit)],
)
async def save_files(
    project_id: UUID, body: SiteSave, user: CurrentUserDep, sites: SiteServiceDep
) -> ApiResponse[SiteSaved]:
    """Commits the files the user changed (and deletes) on top of `baseCommit`. 409 `site_conflict`
    (`details.commitSha`) when the site isn't at `baseCommit` on GitHub any more: nothing is written."""
    return ApiResponse(data=await sites.save(user, project_id, body))


@router.post("/{project_id}/undo", summary="Undo a change", responses=ERRORS)
async def undo(
    project_id: UUID, body: SiteUndo, user: CurrentUserDep, sites: SiteServiceDep
) -> ApiResponse[SiteUndone]:
    """Undoes the changes of `commitSha` (e.g. an AI edit) with a new commit on top of `baseCommit`. 409
    `site_undo_conflict` when a file it changed was changed again since."""
    return ApiResponse(data=await sites.undo(user, project_id, body))
