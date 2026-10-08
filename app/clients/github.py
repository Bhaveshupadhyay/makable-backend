"""GitHub REST API client. Sign-in itself goes through Supabase (`app/clients/supabase.py`)."""

from typing import Protocol

import httpx

from app.core.exceptions import ExternalServiceError
from app.schemas.github import GithubUser

API_URL = "https://api.github.com"
API_VERSION = "2022-11-28"


class GithubError(ExternalServiceError):
    code = "github_error"
    message = "GitHub request failed"


class GithubClient(Protocol):
    async def get_user(self, access_token: str) -> GithubUser: ...


class HttpGithubClient:
    """Talks to api.github.com over a shared `httpx.AsyncClient`, as the user whose token is passed."""

    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http

    async def get_user(self, access_token: str) -> GithubUser:
        """Fetches the authenticated user's profile.

        Raises:
            GithubError: The request failed or the response was unexpected.
        """
        try:
            res = await self._http.get(
                f"{API_URL}/user",
                headers={
                    "Accept": "application/vnd.github+json",
                    "Authorization": f"Bearer {access_token}",
                    "X-GitHub-Api-Version": API_VERSION,
                },
            )
            res.raise_for_status()
            return GithubUser.model_validate(res.json())
        except (httpx.HTTPError, ValueError) as err:  # ValidationError is a ValueError.
            raise GithubError(f"fetching the user failed: {err}") from err
