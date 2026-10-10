"""GitHub REST API client, acting as the user whose token is passed. Sign-in itself goes through Supabase
(`app/clients/supabase.py`)."""

import base64
import time
from typing import Any, Protocol
from urllib.parse import quote

import httpx
from fastapi import status
from pydantic import BaseModel

from app.core.exceptions import ExternalServiceError
from app.schemas.github import (
    GithubBranchHead,
    GithubDirEntry,
    GithubFile,
    GithubRepo,
    GithubTreeChange,
    GithubUser,
)

API_URL = "https://api.github.com"
API_VERSION = "2022-11-28"


class GithubError(ExternalServiceError):
    code = "github_error"
    message = "GitHub request failed"


class GithubUnauthorizedError(GithubError):
    """The stored token was revoked (the user removed the app on GitHub)."""

    status_code = status.HTTP_401_UNAUTHORIZED
    code = "github_reconnect"
    message = "Your GitHub connection has expired. Connect GitHub again."


class GithubRateLimitedError(GithubError):
    """GitHub limited the user's token. `details["retryAfter"]` says how many seconds to wait, from GitHub."""

    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    code = "github_rate_limited"
    message = "GitHub is busy right now. Try again in a few minutes."

    def __init__(self, retry_after: int) -> None:
        super().__init__(details={"retryAfter": retry_after})
        self.retry_after = retry_after


class GithubNotFoundError(GithubError):
    status_code = status.HTTP_404_NOT_FOUND
    code = "github_not_found"
    message = "Not found on GitHub"


class GithubRepoExistsError(GithubError):
    status_code = status.HTTP_409_CONFLICT
    code = "github_repo_exists"
    message = "A repository with that name already exists"


class GithubConflictError(GithubError):
    """A write was based on an older version of the file: it changed on GitHub since it was read."""

    status_code = status.HTTP_409_CONFLICT
    code = "github_conflict"
    message = "The file changed on GitHub"


class GithubClient(Protocol):
    async def get_user(self, access_token: str) -> GithubUser: ...

    async def get_repo(self, access_token: str, full_name: str) -> GithubRepo | None: ...

    async def create_private_repo(self, access_token: str, name: str, description: str) -> GithubRepo: ...

    async def get_file(self, access_token: str, full_name: str, path: str) -> GithubFile | None: ...

    async def list_dir(self, access_token: str, full_name: str, path: str) -> list[GithubDirEntry]: ...

    async def put_file(
        self, access_token: str, full_name: str, path: str, content: str, *, message: str, sha: str | None
    ) -> str: ...

    async def get_branch_head(self, access_token: str, full_name: str, branch: str) -> GithubBranchHead: ...

    async def commit_changes(
        self,
        access_token: str,
        full_name: str,
        branch: str,
        head: GithubBranchHead,
        changes: list[GithubTreeChange],
        *,
        message: str,
    ) -> str: ...


class HttpGithubClient:
    """Talks to api.github.com over a shared `httpx.AsyncClient`, as the user whose token is passed."""

    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http

    async def get_user(self, access_token: str) -> GithubUser:
        """Fetches the authenticated user's profile.

        Raises:
            GithubError: The request failed or the response was unexpected.
        """
        res = await self._request("GET", "/user", access_token)
        return _parse(GithubUser, res, "fetching the user")

    async def get_repo(self, access_token: str, full_name: str) -> GithubRepo | None:
        """A repository, or None if it doesn't exist (or the token can't see it).

        Raises:
            GithubError: The request failed.
        """
        res = await self._request("GET", f"/repos/{full_name}", access_token)
        if res.status_code == status.HTTP_404_NOT_FOUND:
            return None
        return _parse(GithubRepo, res, "fetching the repository")

    async def create_private_repo(self, access_token: str, name: str, description: str) -> GithubRepo:
        """Creates a private repository for the user, with a first commit so files can be written right away.

        Raises:
            GithubRepoExistsError: The user already has a repository with that name.
            GithubError: The request failed.
        """
        body = {
            "name": name,
            "description": description,
            "private": True,
            "auto_init": True,
            "has_issues": False,
            "has_projects": False,
            "has_wiki": False,
        }
        res = await self._request("POST", "/user/repos", access_token, json=body)
        if res.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT:
            raise GithubRepoExistsError()
        return _parse(GithubRepo, res, "creating the repository")

    async def get_file(self, access_token: str, full_name: str, path: str) -> GithubFile | None:
        """A text file on the default branch, or None if there's no file at `path`.

        Raises:
            GithubError: The request failed.
        """
        res = await self._request("GET", _contents_url(full_name, path), access_token)
        if res.status_code == status.HTTP_404_NOT_FOUND:
            return None
        data = _json(res, "reading the file")
        if not isinstance(data, dict) or data.get("type") != "file":
            return None
        sha = str(data["sha"])
        content = data.get("content")
        # Files over 1 MB come without content; read them as a blob instead.
        if data.get("encoding") != "base64" or not content:
            blob = await self._request("GET", f"/repos/{full_name}/git/blobs/{sha}", access_token)
            content = _json(blob, "reading the file")["content"]
        return GithubFile(content=base64.b64decode(content).decode(), sha=sha)

    async def list_dir(self, access_token: str, full_name: str, path: str) -> list[GithubDirEntry]:
        """The entries of a directory on the default branch, or [] if it doesn't exist.

        Raises:
            GithubError: The request failed.
        """
        res = await self._request("GET", _contents_url(full_name, path), access_token)
        if res.status_code == status.HTTP_404_NOT_FOUND:
            return []
        data = _json(res, "listing the directory")
        if not isinstance(data, list):
            return []
        return [GithubDirEntry.model_validate(entry) for entry in data]

    async def put_file(
        self, access_token: str, full_name: str, path: str, content: str, *, message: str, sha: str | None
    ) -> str:
        """Creates or replaces a text file with one commit on the default branch. Returns the new blob SHA.

        `sha` is the SHA of the file being replaced (None to create it). GitHub refuses the write if the file
        has changed since, so a concurrent change is never overwritten.

        Raises:
            GithubConflictError: The file changed since `sha` (or exists although `sha` is None).
            GithubNotFoundError: The repository doesn't exist (yet).
            GithubError: The request failed.
        """
        body: dict[str, Any] = {"message": message, "content": base64.b64encode(content.encode()).decode()}
        if sha is not None:
            body["sha"] = sha
        res = await self._request("PUT", _contents_url(full_name, path), access_token, json=body)
        if res.status_code == status.HTTP_409_CONFLICT or (
            res.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT and sha is None
        ):
            raise GithubConflictError()
        if res.status_code == status.HTTP_404_NOT_FOUND:
            raise GithubNotFoundError()
        return str(_json(res, "writing the file")["content"]["sha"])

    async def get_branch_head(self, access_token: str, full_name: str, branch: str) -> GithubBranchHead:
        """The commit a branch points to, and its tree.

        Raises:
            GithubNotFoundError: The repository or branch doesn't exist (yet).
            GithubError: The request failed.
        """
        res = await self._request("GET", f"/repos/{full_name}/branches/{quote(branch)}", access_token)
        if res.status_code == status.HTTP_404_NOT_FOUND:
            raise GithubNotFoundError()
        data = _json(res, "reading the branch")
        try:
            return GithubBranchHead(commit_sha=data["commit"]["sha"], tree_sha=data["commit"]["commit"]["tree"]["sha"])
        except (KeyError, TypeError) as err:
            raise GithubError("reading the branch failed: unexpected response") from err

    async def commit_changes(
        self,
        access_token: str,
        full_name: str,
        branch: str,
        head: GithubBranchHead,
        changes: list[GithubTreeChange],
        *,
        message: str,
    ) -> str:
        """Writes and deletes several files as one commit on top of `head`, then moves the branch to it. Nothing
        is visible until the branch moves, so a failure part-way leaves the repo unchanged. Returns the commit SHA.

        Raises:
            GithubConflictError: The branch moved since `head` was read (another save got in first).
            GithubError: A request failed.
        """
        tree = [
            {"path": c.path, "mode": "100644", "type": "blob", "content": c.content}
            if c.content is not None
            else {"path": c.path, "mode": "100644", "type": "blob", "sha": None}
            for c in changes
        ]
        res = await self._request(
            "POST", f"/repos/{full_name}/git/trees", access_token, json={"base_tree": head.tree_sha, "tree": tree}
        )
        tree_sha = str(_json(res, "writing the files")["sha"])
        res = await self._request(
            "POST",
            f"/repos/{full_name}/git/commits",
            access_token,
            json={"message": message, "tree": tree_sha, "parents": [head.commit_sha]},
        )
        commit_sha = str(_json(res, "creating the commit")["sha"])
        res = await self._request(
            "PATCH",
            f"/repos/{full_name}/git/refs/heads/{quote(branch)}",
            access_token,
            json={"sha": commit_sha, "force": False},
        )
        # "Update is not a fast forward": the branch moved after `head` was read.
        if res.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT:
            raise GithubConflictError()
        _json(res, "moving the branch")
        return commit_sha

    async def _request(
        self, method: str, path: str, access_token: str, *, json: dict[str, Any] | None = None
    ) -> httpx.Response:
        try:
            res = await self._http.request(
                method,
                f"{API_URL}{path}",
                json=json,
                headers={
                    "Accept": "application/vnd.github+json",
                    "Authorization": f"Bearer {access_token}",
                    "X-GitHub-Api-Version": API_VERSION,
                },
            )
        except httpx.HTTPError as err:
            raise GithubError(f"GitHub couldn't be reached: {err}") from err
        if res.status_code == status.HTTP_401_UNAUTHORIZED:
            raise GithubUnauthorizedError()
        limited = res.status_code == status.HTTP_429_TOO_MANY_REQUESTS or (
            res.status_code == status.HTTP_403_FORBIDDEN
            and ("retry-after" in res.headers or res.headers.get("x-ratelimit-remaining") == "0")
        )
        if limited:
            raise GithubRateLimitedError(_retry_after(res.headers))
        return res


# GitHub's advice when a limit is hit without saying how long to wait (secondary limits).
DEFAULT_RETRY_AFTER = 60


def _retry_after(headers: httpx.Headers) -> int:
    """Seconds to wait before trying again: `retry-after`, else until `x-ratelimit-reset` (epoch seconds)."""
    try:
        if "retry-after" in headers:
            return max(1, int(headers["retry-after"]))
        if "x-ratelimit-reset" in headers:
            return max(1, int(headers["x-ratelimit-reset"]) - int(time.time()))
    except ValueError:
        pass
    return DEFAULT_RETRY_AFTER


def _contents_url(full_name: str, path: str) -> str:
    return f"/repos/{full_name}/contents/{quote(path)}"


def _json(res: httpx.Response, what: str) -> Any:
    if res.is_error:
        raise GithubError(f"{what} failed: HTTP {res.status_code}")
    try:
        return res.json()
    except ValueError as err:
        raise GithubError(f"{what} failed: the response wasn't JSON") from err


def _parse[T: BaseModel](model: type[T], res: httpx.Response, what: str) -> T:
    try:
        return model.model_validate(_json(res, what))
    except ValueError as err:  # ValidationError is a ValueError.
        raise GithubError(f"{what} failed: {err}") from err
