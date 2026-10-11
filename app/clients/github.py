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
from app.core.limits import ServiceBusyError
from app.schemas.github import (
    GithubActionsKey,
    GithubBranchHead,
    GithubDirEntry,
    GithubFile,
    GithubRepo,
    GithubTreeChange,
    GithubUser,
    GithubWorkflowRun,
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


class GithubPagesUnavailableError(GithubError):
    """GitHub refused to turn Pages on for the repo (a private repo on the free plan, or an organisation policy)."""

    status_code = status.HTTP_409_CONFLICT
    code = "github_pages_unavailable"
    message = "GitHub Pages can't be turned on for this repository."


class GithubClient(Protocol):
    async def get_user(self, access_token: str) -> GithubUser: ...

    async def get_scopes(self, access_token: str) -> set[str] | None: ...

    async def get_repo(self, access_token: str, full_name: str) -> GithubRepo | None: ...

    async def create_repo(
        self, access_token: str, name: str, description: str, *, private: bool, homepage: str | None = None
    ) -> GithubRepo: ...

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

    async def get_tree(self, access_token: str, full_name: str, tree_sha: str) -> dict[str, str]: ...

    async def get_blob(self, access_token: str, full_name: str, sha: str) -> str: ...

    async def get_actions_key(self, access_token: str, full_name: str) -> GithubActionsKey: ...

    async def put_actions_secret(
        self, access_token: str, full_name: str, name: str, encrypted_value: str, key_id: str
    ) -> None: ...

    async def has_actions_secret(self, access_token: str, full_name: str, name: str) -> bool: ...

    async def enable_pages_workflow(self, access_token: str, full_name: str) -> None: ...

    async def dispatch_workflow(self, access_token: str, full_name: str, workflow: str, ref: str) -> None: ...

    async def latest_workflow_run(
        self, access_token: str, full_name: str, workflow: str, branch: str, *, created_since: str | None = None
    ) -> GithubWorkflowRun | None: ...


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

    async def get_scopes(self, access_token: str) -> set[str] | None:
        """The OAuth scopes the token was granted, from GitHub's `X-OAuth-Scopes` header. None when GitHub doesn't
        say (tokens that aren't OAuth App tokens).

        Raises:
            GithubError: The request failed.
        """
        res = await self._request("GET", "/user", access_token)
        _json(res, "fetching the user")
        header = res.headers.get("x-oauth-scopes")
        if header is None:
            return None
        return {scope.strip() for scope in header.split(",") if scope.strip()}

    async def create_repo(
        self, access_token: str, name: str, description: str, *, private: bool, homepage: str | None = None
    ) -> GithubRepo:
        """Creates a repository for the user, with a first commit so files can be written right away.

        Raises:
            GithubRepoExistsError: The user already has a repository with that name.
            GithubError: The request failed.
        """
        body: dict[str, Any] = {
            "name": name,
            "description": description,
            "private": private,
            "auto_init": True,
            "has_issues": False,
            "has_projects": False,
            "has_wiki": False,
        }
        if homepage:
            body["homepage"] = homepage
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
        Binary files are uploaded as blobs first (a tree only takes text inline).

        Raises:
            GithubConflictError: The branch moved since `head` was read (another save got in first).
            GithubError: A request failed.
        """
        tree: list[dict[str, Any]] = []
        for c in changes:
            entry: dict[str, Any] = {"path": c.path, "mode": "100644", "type": "blob"}
            if isinstance(c.content, bytes):
                entry["sha"] = await self._create_blob(access_token, full_name, c.content)
            elif c.content is not None:
                entry["content"] = c.content
            else:
                entry["sha"] = None
            tree.append(entry)
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

    async def get_tree(self, access_token: str, full_name: str, tree_sha: str) -> dict[str, str]:
        """Every file in a tree, recursively: path -> blob SHA.

        Raises:
            GithubError: The request failed, or the tree is too big for GitHub to list in one answer.
        """
        res = await self._request("GET", f"/repos/{full_name}/git/trees/{tree_sha}?recursive=1", access_token)
        data = _json(res, "reading the files")
        if data.get("truncated"):
            raise GithubError("reading the files failed: the repository has too many files")
        return {str(e["path"]): str(e["sha"]) for e in data.get("tree", []) if e.get("type") == "blob"}

    async def get_blob(self, access_token: str, full_name: str, sha: str) -> str:
        """A text file's contents by its blob SHA (which never changes, unlike a path on a branch).

        Raises:
            GithubNotFoundError: No such blob.
            GithubError: The request failed, or the file isn't UTF-8 text.
        """
        res = await self._request("GET", f"/repos/{full_name}/git/blobs/{sha}", access_token)
        if res.status_code == status.HTTP_404_NOT_FOUND:
            raise GithubNotFoundError()
        try:
            return base64.b64decode(_json(res, "reading the file")["content"]).decode()
        except (KeyError, TypeError, ValueError) as err:
            raise GithubError("reading the file failed: not a text file") from err

    async def get_actions_key(self, access_token: str, full_name: str) -> GithubActionsKey:
        """The public key the repo's Actions secrets are encrypted with.

        Raises:
            GithubError: The request failed.
        """
        res = await self._request("GET", f"/repos/{full_name}/actions/secrets/public-key", access_token)
        return _parse(GithubActionsKey, res, "reading the secrets key")

    async def put_actions_secret(
        self, access_token: str, full_name: str, name: str, encrypted_value: str, key_id: str
    ) -> None:
        """Creates or replaces an Actions secret. `encrypted_value` is sealed with the repo's key (base64).

        Raises:
            GithubError: The request failed.
        """
        res = await self._request(
            "PUT",
            f"/repos/{full_name}/actions/secrets/{quote(name)}",
            access_token,
            json={"encrypted_value": encrypted_value, "key_id": key_id},
        )
        if res.is_error:
            raise GithubError(f"saving the secret failed: HTTP {res.status_code}")

    async def has_actions_secret(self, access_token: str, full_name: str, name: str) -> bool:
        """Whether the repo has an Actions secret with this name (GitHub never gives its value back).

        Raises:
            GithubError: The request failed.
        """
        res = await self._request("GET", f"/repos/{full_name}/actions/secrets/{quote(name)}", access_token)
        if res.status_code == status.HTTP_404_NOT_FOUND:
            return False
        _json(res, "reading the secret")
        return True

    async def enable_pages_workflow(self, access_token: str, full_name: str) -> None:
        """Turns GitHub Pages on, deployed by a workflow (or switches it to that if it's on already).

        Raises:
            GithubPagesUnavailableError: GitHub won't turn Pages on for this repo.
            GithubError: The request failed.
        """
        body = {"build_type": "workflow"}
        res = await self._request("POST", f"/repos/{full_name}/pages", access_token, json=body)
        if res.status_code == status.HTTP_409_CONFLICT:  # Already on.
            res = await self._request("PUT", f"/repos/{full_name}/pages", access_token, json=body)
        if res.status_code in (status.HTTP_403_FORBIDDEN, status.HTTP_422_UNPROCESSABLE_CONTENT):
            raise GithubPagesUnavailableError()
        if res.is_error:
            raise GithubError(f"turning on GitHub Pages failed: HTTP {res.status_code}")

    async def dispatch_workflow(self, access_token: str, full_name: str, workflow: str, ref: str) -> None:
        """Starts a workflow that has a `workflow_dispatch` trigger.

        Raises:
            GithubNotFoundError: The workflow isn't on that branch, or GitHub hasn't registered it (or its
                `workflow_dispatch` trigger) yet: a workflow file that was just pushed can take a few seconds.
            GithubError: The request failed.
        """
        res = await self._request(
            "POST",
            f"/repos/{full_name}/actions/workflows/{quote(workflow)}/dispatches",
            access_token,
            json={"ref": ref},
        )
        # 422 "Workflow does not have 'workflow_dispatch' trigger": GitHub still has the file's previous version.
        if res.status_code in (status.HTTP_404_NOT_FOUND, status.HTTP_422_UNPROCESSABLE_CONTENT):
            raise GithubNotFoundError()
        if res.is_error:
            raise GithubError(f"starting the workflow failed: HTTP {res.status_code}")

    async def latest_workflow_run(
        self, access_token: str, full_name: str, workflow: str, branch: str, *, created_since: str | None = None
    ) -> GithubWorkflowRun | None:
        """The newest run of a workflow on a branch, or None if it never ran (or doesn't exist). With
        `created_since` (ISO 8601, UTC), only a run created then or later.

        Raises:
            GithubError: The request failed.
        """
        query = f"branch={quote(branch)}&per_page=1"
        if created_since:
            query += f"&created={quote('>=' + created_since)}"
        res = await self._request(
            "GET", f"/repos/{full_name}/actions/workflows/{quote(workflow)}/runs?{query}", access_token
        )
        if res.status_code == status.HTTP_404_NOT_FOUND:
            return None
        runs = _json(res, "reading the workflow runs").get("workflow_runs", [])
        if not runs:
            return None
        try:
            return GithubWorkflowRun.model_validate(runs[0])
        except ValueError as err:
            raise GithubError(f"reading the workflow runs failed: {err}") from err

    async def _create_blob(self, access_token: str, full_name: str, data: bytes) -> str:
        res = await self._request(
            "POST",
            f"/repos/{full_name}/git/blobs",
            access_token,
            json={"content": base64.b64encode(data).decode(), "encoding": "base64"},
        )
        return str(_json(res, "uploading a file")["sha"])

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
        except httpx.PoolTimeout as err:
            # Every connection to GitHub from this worker is busy: back off rather than wait.
            raise ServiceBusyError() from err
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
