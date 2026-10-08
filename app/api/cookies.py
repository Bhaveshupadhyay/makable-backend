"""Auth cookies. The SPA never sees tokens: they live in HttpOnly cookies set and cleared here."""

from datetime import timedelta

from fastapi import Response

from app.constants.auth import (
    ACCESS_TOKEN_COOKIE,
    ACCESS_TOKEN_COOKIE_PATH,
    OAUTH_STATE_COOKIE,
    OAUTH_STATE_COOKIE_PATH,
    OAUTH_STATE_TTL_SECONDS,
    REFRESH_TOKEN_COOKIE,
    REFRESH_TOKEN_COOKIE_PATH,
)
from app.schemas.auth import SessionTokens


class AuthCookies:
    """Sets and clears the auth cookies with consistent flags. SameSite=Lax is required: the OAuth
    callback is a top-level navigation from Supabase, and Strict cookies wouldn't be sent."""

    def __init__(self, *, secure: bool, refresh_ttl: timedelta) -> None:
        self._secure = secure
        self._refresh_max_age = int(refresh_ttl.total_seconds())

    def set_tokens(self, response: Response, tokens: SessionTokens) -> None:
        """The access cookie lives exactly as long as the Supabase access token inside it."""
        self._set(response, ACCESS_TOKEN_COOKIE, tokens.access_token, tokens.expires_in, ACCESS_TOKEN_COOKIE_PATH)
        self._set(
            response, REFRESH_TOKEN_COOKIE, tokens.refresh_token, self._refresh_max_age, REFRESH_TOKEN_COOKIE_PATH
        )

    def clear_tokens(self, response: Response) -> None:
        self._clear(response, ACCESS_TOKEN_COOKIE, ACCESS_TOKEN_COOKIE_PATH)
        self._clear(response, REFRESH_TOKEN_COOKIE, REFRESH_TOKEN_COOKIE_PATH)

    def set_oauth_state(self, response: Response, value: str) -> None:
        self._set(response, OAUTH_STATE_COOKIE, value, OAUTH_STATE_TTL_SECONDS, OAUTH_STATE_COOKIE_PATH)

    def clear_oauth_state(self, response: Response) -> None:
        self._clear(response, OAUTH_STATE_COOKIE, OAUTH_STATE_COOKIE_PATH)

    def _set(self, response: Response, key: str, value: str, max_age: int, path: str) -> None:
        response.set_cookie(key, value, max_age=max_age, path=path, httponly=True, secure=self._secure, samesite="lax")

    def _clear(self, response: Response, key: str, path: str) -> None:
        response.delete_cookie(key, path=path, httponly=True, secure=self._secure, samesite="lax")
