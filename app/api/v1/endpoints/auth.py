import logging
from typing import Annotated, Any

from fastapi import APIRouter, Cookie, Query, Response, status
from fastapi.responses import RedirectResponse

from app.api.dependencies import (
    AccessTokenDep,
    AuthCookiesDep,
    AuthServiceDep,
    CurrentUserDep,
    RefreshTokenDep,
    SettingsDep,
)
from app.constants.auth import OAUTH_STATE_COOKIE
from app.core.exceptions import LoginError, UnauthorizedError
from app.schemas.auth import SessionRead
from app.schemas.common import ApiResponse, ErrorResponse
from app.utils.return_to import with_query

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

UNAUTHORIZED: dict[int | str, dict[str, Any]] = {status.HTTP_401_UNAUTHORIZED: {"model": ErrorResponse}}


@router.get(
    "/github/login",
    status_code=status.HTTP_302_FOUND,
    summary="Start GitHub sign-in",
    response_class=RedirectResponse,
)
async def github_login(
    auth: AuthServiceDep,
    cookies: AuthCookiesDep,
    return_to: Annotated[str | None, Query(alias="returnTo", max_length=2048)] = None,
) -> RedirectResponse:
    """Redirects to GitHub through Supabase Auth. `returnTo` is the SPA path to come back to (unsafe
    values become `/`)."""
    login = auth.start_login(return_to)
    response = RedirectResponse(login.authorize_url, status.HTTP_302_FOUND)
    cookies.set_oauth_state(response, login.state_cookie)
    return response


@router.get(
    "/github/callback",
    status_code=status.HTTP_302_FOUND,
    summary="Sign-in callback",
    response_class=RedirectResponse,
)
async def github_callback(
    auth: AuthServiceDep,
    cookies: AuthCookiesDep,
    settings: SettingsDep,
    code: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
    state_cookie: Annotated[str | None, Cookie(alias=OAUTH_STATE_COOKIE)] = None,
) -> RedirectResponse:
    """Supabase sends the browser here after GitHub. Signs the user in and redirects back to the SPA.
    This is a browser navigation, so failures also redirect, with `?authError=<code>`, instead of
    returning a JSON error."""
    try:
        result = await auth.complete_login(
            state_cookie=state_cookie, code=code, error=error, error_description=error_description
        )
    except LoginError as err:
        logger.warning("GitHub sign-in failed", extra={"code": err.code, "error": err.message})
        response = RedirectResponse(
            settings.app_url + with_query(err.return_to, authError=err.code), status.HTTP_302_FOUND
        )
    else:
        response = RedirectResponse(settings.app_url + result.return_to, status.HTTP_302_FOUND)
        cookies.set_tokens(response, result.tokens)
    cookies.clear_oauth_state(response)
    return response


@router.get("/session", summary="The signed-in user", responses=UNAUTHORIZED)
async def get_session(user: CurrentUserDep) -> ApiResponse[SessionRead]:
    """Returns the signed-in user, or 401. On 401 the SPA should try `POST /auth/refresh` once."""
    return ApiResponse(data=SessionRead(user=user))


@router.post("/refresh", status_code=status.HTTP_204_NO_CONTENT, summary="Refresh the session", responses=UNAUTHORIZED)
async def refresh(auth: AuthServiceDep, cookies: AuthCookiesDep, refresh_token: RefreshTokenDep = None) -> Response:
    """Rotates the refresh token cookie and issues a new access token cookie."""
    if not refresh_token:
        raise UnauthorizedError()
    tokens = await auth.refresh(refresh_token)
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    cookies.set_tokens(response, tokens)
    return response


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT, summary="Sign out")
async def logout(
    auth: AuthServiceDep,
    cookies: AuthCookiesDep,
    access_token: AccessTokenDep = None,
    refresh_token: RefreshTokenDep = None,
) -> Response:
    """Ends the Supabase session and clears the cookies. Safe to call when signed out."""
    await auth.logout(access_token=access_token, refresh_token=refresh_token)
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    cookies.clear_tokens(response)
    return response
