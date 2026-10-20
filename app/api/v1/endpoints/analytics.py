import logging
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Cookie, Depends, Path, Query, Response, status
from fastapi.responses import RedirectResponse

from app.api.dependencies import (
    AnalyticsServiceDep,
    AnalyticsSlotsDep,
    AuthCookiesDep,
    CurrentUserDep,
    SettingsDep,
    analytics_list_limit,
    analytics_slot,
)
from app.constants.analytics import GA_ID, GOOGLE_STATE_COOKIE, ReportRange
from app.core.limits import ServiceBusyError
from app.schemas.analytics import (
    AnalyticsReport,
    GaAccountRead,
    GaStreamRead,
    GoogleConnection,
    GoogleConnectRedirect,
    GoogleConnectRequest,
    SiteAnalyticsLink,
    SiteAnalyticsRead,
    SiteAnalyticsStatus,
)
from app.schemas.common import ApiResponse, ErrorResponse
from app.services.analytics_service import GoogleConnectError
from app.utils.return_to import with_query

logger = logging.getLogger(__name__)

# Requests that wait on Google hold one of the worker's slots. The callback takes its own (`callback_router`), so a
# busy worker still redirects it back to the SPA.
router = APIRouter(tags=["analytics"], dependencies=[Depends(analytics_slot)])
callback_router = APIRouter(tags=["analytics"])

ERRORS: dict[int | str, dict[str, Any]] = {
    code: {"model": ErrorResponse}
    for code in (
        status.HTTP_401_UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN,
        status.HTTP_404_NOT_FOUND,
        status.HTTP_409_CONFLICT,
        status.HTTP_429_TOO_MANY_REQUESTS,
        status.HTTP_502_BAD_GATEWAY,
        status.HTTP_503_SERVICE_UNAVAILABLE,
    )
}

PropertyId = Annotated[str, Path(pattern=f"^{GA_ID.pattern}$")]


@router.get("/analytics/google", summary="The user's Google Analytics connection", responses=ERRORS)
async def get_connection(user: CurrentUserDep, analytics: AnalyticsServiceDep) -> ApiResponse[GoogleConnection]:
    return ApiResponse(data=await analytics.connection(user))


@router.post("/analytics/google/connect", summary="Start connecting Google Analytics", responses=ERRORS)
async def connect(
    body: GoogleConnectRequest,
    response: Response,
    user: CurrentUserDep,
    analytics: AnalyticsServiceDep,
    cookies: AuthCookiesDep,
) -> ApiResponse[GoogleConnectRedirect]:
    """Returns Google's consent page for the SPA to send the browser to, and sets the state cookie its callback
    checks. A request rather than a navigation, so an expired session is refreshed first. 503
    `analytics_not_configured` without makable's Google OAuth client."""
    start = analytics.start_connect(user, body.return_to)
    cookies.set_google_state(response, start.state_cookie)
    return ApiResponse(data=GoogleConnectRedirect(authorize_url=start.authorize_url))


@callback_router.get(
    "/analytics/google/callback",
    status_code=status.HTTP_302_FOUND,
    summary="Google Analytics connect callback",
    response_class=RedirectResponse,
)
async def callback(
    analytics: AnalyticsServiceDep,
    cookies: AuthCookiesDep,
    settings: SettingsDep,
    slots: AnalyticsSlotsDep,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    state_cookie: Annotated[str | None, Cookie(alias=GOOGLE_STATE_COOKIE)] = None,
) -> RedirectResponse:
    """Google sends the browser here after the consent page. Keeps the user's token and redirects back to the SPA
    with `?analytics=connected`. This is a browser navigation, so failures also redirect, with
    `?analyticsError=<code>`, instead of returning a JSON error (`service_busy` when the worker has no slot)."""
    try:
        async with slots.slot():
            return_to = await analytics.complete_connect(state_cookie=state_cookie, code=code, state=state, error=error)
    except GoogleConnectError as err:
        logger.warning("Google Analytics connect failed", extra={"code": err.code, "error": err.message})
        location = with_query(err.return_to, analyticsError=err.code)
    except ServiceBusyError as err:
        logger.warning("Google Analytics connect refused: the worker is busy")
        location = with_query(analytics.return_path(state_cookie), analyticsError=err.code)
    else:
        location = with_query(return_to, analytics="connected")
    response = RedirectResponse(settings.app_url + location, status.HTTP_302_FOUND)
    cookies.clear_google_state(response)
    return response


@router.delete(
    "/analytics/google", summary="Disconnect Google Analytics", status_code=status.HTTP_204_NO_CONTENT, responses=ERRORS
)
async def disconnect(user: CurrentUserDep, analytics: AnalyticsServiceDep) -> None:
    """Revokes makable's access at Google and forgets the token. Sites keep their streams (and their pages the tag)."""
    await analytics.disconnect(user)


@router.get(
    "/analytics/google/properties",
    summary="The user's GA4 properties",
    responses=ERRORS,
    dependencies=[Depends(analytics_list_limit)],
)
async def list_properties(user: CurrentUserDep, analytics: AnalyticsServiceDep) -> ApiResponse[list[GaAccountRead]]:
    """The Google account's Analytics accounts and their GA4 properties. 409 `google_not_connected` before the user
    connects, `google_reconnect` when Google refused the stored token."""
    return ApiResponse(data=await analytics.accounts(user))


@router.get(
    "/analytics/google/properties/{property_id}/streams",
    summary="A property's web streams",
    responses=ERRORS,
    dependencies=[Depends(analytics_list_limit)],
)
async def list_streams(
    property_id: PropertyId, user: CurrentUserDep, analytics: AnalyticsServiceDep
) -> ApiResponse[list[GaStreamRead]]:
    return ApiResponse(data=await analytics.streams(user, property_id))


@router.get("/sites/{project_id}/analytics", summary="A site's analytics setup", responses=ERRORS)
async def get_site_analytics(
    project_id: UUID, user: CurrentUserDep, analytics: AnalyticsServiceDep
) -> ApiResponse[SiteAnalyticsStatus]:
    """The Google connection and the stream the site reports to (null until it's picked). `link.tagPublished` says
    whether the site's deploy has the stream's tag yet."""
    return ApiResponse(data=await analytics.site_status(user, project_id))


@router.put(
    "/sites/{project_id}/analytics",
    summary="Pick the stream a site reports to",
    responses=ERRORS,
    dependencies=[Depends(analytics_list_limit)],
)
async def link_site(
    project_id: UUID, body: SiteAnalyticsLink, user: CurrentUserDep, analytics: AnalyticsServiceDep
) -> ApiResponse[SiteAnalyticsRead]:
    """Checks the property and web stream with Google and links them. The next publish adds the tag to the site."""
    return ApiResponse(data=await analytics.link_site(user, project_id, body))


@router.delete(
    "/sites/{project_id}/analytics",
    summary="Stop a site's analytics",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=ERRORS,
)
async def unlink_site(project_id: UUID, user: CurrentUserDep, analytics: AnalyticsServiceDep) -> None:
    """Unlinks the stream. The tag leaves the site with its next publish."""
    await analytics.unlink_site(user, project_id)


@router.get("/sites/{project_id}/analytics/report", summary="A site's traffic", responses=ERRORS)
async def get_report(
    project_id: UUID,
    user: CurrentUserDep,
    analytics: AnalyticsServiceDep,
    range_: Annotated[ReportRange, Query(alias="range")] = ReportRange.LAST_28_DAYS,
) -> ApiResponse[AnalyticsReport]:
    """Visitors, page views and sessions over the range (and the same days before), day by day, the top pages,
    sources, countries and devices, and the visitors of the last 30 minutes. Read from Google at most every few
    minutes. 404 `analytics_not_linked` before a stream is picked."""
    return ApiResponse(data=await analytics.report(user, project_id, range_))
