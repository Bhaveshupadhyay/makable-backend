"""Hosting platforms' APIs, used when the user pastes a platform token in the chat: check the token and create the
project the deploy workflow deploys to. Only these fixed hosts are ever called. The token is only used for these
requests; it's never stored or logged."""

from typing import Any, Protocol

import httpx
from fastapi import status
from pydantic import BaseModel

from app.constants.publish import TARGET_NAMES, PublishTarget
from app.core.exceptions import AppError, ConflictError, ExternalServiceError
from app.core.limits import ServiceBusyError

VERCEL_API = "https://api.vercel.com"
CLOUDFLARE_API = "https://api.cloudflare.com/client/v4"
NETLIFY_API = "https://api.netlify.com/api/v1"


class PlatformError(ExternalServiceError):
    code = "platform_error"
    message = "The hosting platform didn't answer as expected. Try again shortly."


class PlatformTokenInvalidError(AppError):
    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    code = "platform_token_invalid"
    message = "The hosting platform didn't accept that token."


class PlatformAccountRequiredError(AppError):
    """The Cloudflare token can see several accounts. `details["accounts"]` lists them (`id`, `name`)."""

    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    code = "platform_account_required"
    message = "That token can use several Cloudflare accounts. Pick the one to publish to."


class PlatformProjectTakenError(ConflictError):
    code = "platform_project_taken"
    message = "That project name is taken on the hosting platform. Pick another name."


class PreparedProject(BaseModel):
    """A platform project that exists and the token can deploy to, and the Actions secrets the workflow needs."""

    site_url: str
    secrets: dict[str, str]


class DeployPlatformClient(Protocol):
    async def prepare(
        self, target: PublishTarget, token: str, *, project_name: str, branch: str, account_id: str | None
    ) -> PreparedProject: ...


class HttpDeployPlatformClient:
    """Talks to Vercel, Cloudflare and Netlify over the shared `httpx.AsyncClient`."""

    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http

    async def prepare(
        self, target: PublishTarget, token: str, *, project_name: str, branch: str, account_id: str | None
    ) -> PreparedProject:
        """Checks the token and creates the project if the user doesn't have it yet.

        Raises:
            PlatformTokenInvalidError: The platform refused the token (or it lacks a permission).
            PlatformAccountRequiredError: Cloudflare: the token sees several accounts and none was picked.
            PlatformProjectTakenError: The project name belongs to someone else.
            PlatformError: The platform failed.
        """
        match target:
            case PublishTarget.VERCEL:
                return await self._vercel(token, project_name)
            case PublishTarget.CLOUDFLARE_PAGES:
                return await self._cloudflare(token, project_name, branch, account_id)
            case PublishTarget.NETLIFY:
                return await self._netlify(token, project_name)
            case _:
                raise ValueError(f"{target} takes no token")

    async def _vercel(self, token: str, name: str) -> PreparedProject:
        target = PublishTarget.VERCEL
        _check_user(await self._request(target, "GET", f"{VERCEL_API}/v2/user", token), target)
        res = await self._request(target, "GET", f"{VERCEL_API}/v9/projects/{name}", token)
        if res.status_code == status.HTTP_404_NOT_FOUND:
            res = await self._request(
                target, "POST", f"{VERCEL_API}/v11/projects", token, json={"name": name, "framework": None}
            )
            if res.status_code == status.HTTP_409_CONFLICT:
                raise PlatformProjectTakenError()
        _check_token(res, target)
        _json(res, target)
        return PreparedProject(site_url=f"https://{name}.vercel.app", secrets={"VERCEL_TOKEN": token})

    async def _cloudflare(self, token: str, name: str, branch: str, account_id: str | None) -> PreparedProject:
        target = PublishTarget.CLOUDFLARE_PAGES
        if account_id is None:
            res = await self._request(target, "GET", f"{CLOUDFLARE_API}/accounts", token)
            _check_token(res, target)
            accounts = _json(res, target).get("result") or []
            if not accounts:
                raise PlatformTokenInvalidError("That Cloudflare token can't use any account.")
            if len(accounts) > 1:
                listed = [{"id": a.get("id"), "name": a.get("name")} for a in accounts]
                raise PlatformAccountRequiredError(details={"accounts": listed})
            account_id = str(accounts[0]["id"])
        projects = f"{CLOUDFLARE_API}/accounts/{account_id}/pages/projects"
        res = await self._request(target, "GET", f"{projects}/{name}", token)
        if res.status_code == status.HTTP_404_NOT_FOUND:
            res = await self._request(target, "POST", projects, token, json={"name": name, "production_branch": branch})
            if res.status_code == status.HTTP_409_CONFLICT:
                raise PlatformProjectTakenError()
        _check_token(res, target, permission="Cloudflare Pages: Edit")
        subdomain = (_json(res, target).get("result") or {}).get("subdomain") or f"{name}.pages.dev"
        return PreparedProject(
            site_url=f"https://{subdomain}",
            secrets={"CLOUDFLARE_API_TOKEN": token, "CLOUDFLARE_ACCOUNT_ID": account_id},
        )

    async def _netlify(self, token: str, name: str) -> PreparedProject:
        target = PublishTarget.NETLIFY
        _check_user(await self._request(target, "GET", f"{NETLIFY_API}/user", token), target)
        # A site's id can be its domain; this only finds sites the token can see.
        res = await self._request(target, "GET", f"{NETLIFY_API}/sites/{name}.netlify.app", token)
        if res.status_code == status.HTTP_404_NOT_FOUND:
            res = await self._request(target, "POST", f"{NETLIFY_API}/sites", token, json={"name": name})
            if res.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT:
                raise PlatformProjectTakenError()
        _check_token(res, target)
        site = _json(res, target)
        site_url = site.get("ssl_url") or site.get("url") or f"https://{name}.netlify.app"
        return PreparedProject(site_url=str(site_url), secrets={"NETLIFY_AUTH_TOKEN": token})

    async def _request(
        self, target: PublishTarget, method: str, url: str, token: str, *, json: dict[str, Any] | None = None
    ) -> httpx.Response:
        try:
            return await self._http.request(
                method, url, json=json, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"}
            )
        except httpx.PoolTimeout as err:
            raise ServiceBusyError() from err
        except httpx.HTTPError as err:
            raise PlatformError(f"{TARGET_NAMES[target]} couldn't be reached.") from err


def _check_token(res: httpx.Response, target: PublishTarget, *, permission: str | None = None) -> None:
    if res.status_code in (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN):
        needs = f" It needs the {permission} permission." if permission else ""
        raise PlatformTokenInvalidError(f"{TARGET_NAMES[target]} didn't accept that token.{needs}")


def _check_user(res: httpx.Response, target: PublishTarget) -> None:
    """The token check: the platform's answer for the token's own user."""
    _check_token(res, target)
    _json(res, target)


def _json(res: httpx.Response, target: PublishTarget) -> Any:
    name = TARGET_NAMES[target]
    if res.status_code == status.HTTP_429_TOO_MANY_REQUESTS:
        raise PlatformError(f"{name} is limiting requests right now. Try again in a few minutes.")
    if res.is_error:
        raise PlatformError(f"{name} answered HTTP {res.status_code}.")
    try:
        data = res.json()
    except ValueError as err:
        raise PlatformError(f"{name} answered with something that isn't JSON.") from err
    if not isinstance(data, dict):
        raise PlatformError(f"{name} answered with something unexpected.")
    return data
