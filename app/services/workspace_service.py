import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from uuid import UUID

from fastapi import status
from pydantic import ValidationError

from app.clients.github import GithubClient, GithubConflictError, GithubNotFoundError, GithubRepoExistsError
from app.constants.workspace import (
    MARKER_CONTENT,
    MARKER_PATH,
    MAX_PROJECTS,
    MAX_SESSION_BYTES,
    NEW_REPO_RETRY_DELAYS,
    PROJECTS_DIR,
    VERIFIED_REPO_TTL,
    WORKSPACE_DESCRIPTION,
    WORKSPACE_REPO,
    session_path,
)
from app.core.exceptions import AppError, ConflictError, NotFoundError
from app.core.security import TokenCipher
from app.repositories.github_credential import GithubCredentialRepository
from app.schemas.github import GithubRepo
from app.schemas.session import SessionSnapshot
from app.schemas.user import UserRead
from app.schemas.workspace import WorkspaceSaved, WorkspaceSession, WorkspaceSessionWrite

logger = logging.getLogger(__name__)


class GithubNotConnectedError(AppError):
    status_code = status.HTTP_401_UNAUTHORIZED
    code = "github_reconnect"
    message = "Connect GitHub again to save your chat there."


class WorkspaceRepoTakenError(ConflictError):
    code = "workspace_repo_taken"
    message = (
        f"You already have a repository named {WORKSPACE_REPO} that makable didn't create, so it won't write to it. "
        "Rename or delete it on GitHub to turn on saving to GitHub."
    )


class WorkspaceRepoPublicError(ConflictError):
    code = "workspace_repo_public"
    message = (
        f"Your {WORKSPACE_REPO} repository is public, so saving your chat there would make it public. "
        "Make it private on GitHub to keep saving."
    )


class WorkspaceConflictError(ConflictError):
    code = "workspace_conflict"
    message = "This session was changed on another device since it was last saved here."


class WorkspaceSessionNotFoundError(NotFoundError):
    code = "workspace_session_not_found"
    message = "No saved session on GitHub yet"


class WorkspaceSessionTooLargeError(AppError):
    status_code = status.HTTP_413_CONTENT_TOO_LARGE
    code = "workspace_session_too_large"
    message = "This session is too large to save to GitHub."


class WorkspaceInvalidError(AppError):
    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    code = "workspace_invalid"
    message = "That session doesn't belong here"


class VerifiedRepos:
    """Workspace repos checked recently (exist, private, have the marker), per user and login, so saves skip
    the two checking requests. Process-wide: each worker keeps its own. Entries expire after `ttl` seconds."""

    MAX_ENTRIES = 10_000

    def __init__(self, ttl: float = VERIFIED_REPO_TTL, clock: Callable[[], float] = time.monotonic) -> None:
        self._ttl = ttl
        self._clock = clock
        self._repos: dict[tuple[UUID, str], tuple[GithubRepo, float]] = {}

    def get(self, user: UserRead) -> GithubRepo | None:
        entry = self._repos.get((user.id, user.login))
        if entry is None:
            return None
        if entry[1] <= self._clock():
            self.discard(user)
            return None
        return entry[0]

    def add(self, user: UserRead, repo: GithubRepo) -> None:
        now = self._clock()
        if len(self._repos) >= self.MAX_ENTRIES:
            self._repos = {key: entry for key, entry in self._repos.items() if entry[1] > now}
        self._repos[(user.id, user.login)] = (repo, now + self._ttl)

    def discard(self, user: UserRead) -> None:
        self._repos.pop((user.id, user.login), None)

    def clear(self) -> None:
        self._repos.clear()


VERIFIED_REPOS = VerifiedRepos()


class WorkspaceService:
    """Keeps the user's builder sessions in a private `makable-workspace` repo on their GitHub account, one file
    per site (`projects/<projectId>/session.json`). The browser keeps the working copy; nothing is stored here.

    makable only writes to a workspace repo it created (it has the marker file) and that is private.
    """

    def __init__(
        self,
        github: GithubClient,
        credentials: GithubCredentialRepository,
        cipher: TokenCipher,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        retry_delays: Sequence[float] = NEW_REPO_RETRY_DELAYS,
        verified: VerifiedRepos = VERIFIED_REPOS,
    ) -> None:
        self._github = github
        self._credentials = credentials
        self._cipher = cipher
        self._sleep = sleep
        self._retry_delays = retry_delays
        self._verified = verified

    async def get_session(self, user: UserRead, project_id: UUID) -> WorkspaceSession:
        """The session saved for a site.

        Raises:
            WorkspaceSessionNotFoundError: Nothing is saved for that site (or there's no workspace repo yet).
            WorkspaceRepoTakenError: The repo exists but makable didn't create it.
            WorkspaceRepoPublicError: The repo is public.
            GithubNotConnectedError: No usable GitHub token.
            GithubError: GitHub failed.
        """
        token = await self._token(user)
        repo = await self._workspace(token, user)
        session = await self._read(token, repo, session_path(str(project_id))) if repo else None
        if session is None:
            raise WorkspaceSessionNotFoundError()
        return session

    async def get_latest_session(self, user: UserRead) -> WorkspaceSession:
        """The most recently saved session of any of the user's sites, to restore on a new device.

        Raises:
            WorkspaceSessionNotFoundError: Nothing is saved yet.
            WorkspaceRepoTakenError: The repo exists but makable didn't create it.
            WorkspaceRepoPublicError: The repo is public.
            GithubNotConnectedError: No usable GitHub token.
            GithubError: GitHub failed.
        """
        token = await self._token(user)
        repo = await self._workspace(token, user)
        if repo is None:
            raise WorkspaceSessionNotFoundError()
        entries = await self._github.list_dir(token, repo.full_name, PROJECTS_DIR)
        folders = [e.name for e in entries if e.type == "dir"][:MAX_PROJECTS]
        sessions = await asyncio.gather(*(self._read(token, repo, session_path(f)) for f in folders))
        found = [s for s in sessions if s is not None]
        if not found:
            raise WorkspaceSessionNotFoundError()
        return max(found, key=lambda s: s.snapshot.exported_at)

    async def save_session(self, user: UserRead, project_id: UUID, write: WorkspaceSessionWrite) -> WorkspaceSaved:
        """Saves a site's session as one commit, creating the private workspace repo on first use.

        Raises:
            WorkspaceInvalidError: The session is for another site than `project_id`.
            WorkspaceSessionTooLargeError: The session is over the size limit.
            WorkspaceConflictError: The saved file changed since `write.base_sha` (another device saved).
            WorkspaceRepoTakenError: The repo exists but makable didn't create it.
            WorkspaceRepoPublicError: The repo is public.
            GithubNotConnectedError: No usable GitHub token.
            GithubError: GitHub failed.
        """
        if write.snapshot.project_id != project_id:
            raise WorkspaceInvalidError()
        content = _serialize(write.snapshot)
        if len(content.encode()) > MAX_SESSION_BYTES:
            raise WorkspaceSessionTooLargeError()
        token = await self._token(user)
        path = session_path(str(project_id))
        message = f"Save session ({len(write.snapshot.conversation.messages)} messages)"
        repo = await self._workspace(token, user) or await self._create_workspace(token, user)
        try:
            try:
                sha = await self._github.put_file(
                    token, repo.full_name, path, content, message=message, sha=write.base_sha
                )
            except GithubNotFoundError:
                # The repo checked earlier is gone (deleted or renamed on GitHub): check again, once.
                self._verified.discard(user)
                repo = await self._workspace(token, user) or await self._create_workspace(token, user)
                sha = await self._github.put_file(
                    token, repo.full_name, path, content, message=message, sha=write.base_sha
                )
        except GithubConflictError:
            current = await self._read(token, repo, path)
            raise WorkspaceConflictError(
                details={"sha": current.sha, "exportedAt": current.snapshot.exported_at} if current else None
            ) from None
        logger.info("workspace session saved", extra={"user_id": str(user.id), "project_id": str(project_id)})
        return WorkspaceSaved(sha=sha, repo_url=repo.html_url)

    async def _token(self, user: UserRead) -> str:
        credential = await self._credentials.get(user.id)
        if credential is None:
            raise GithubNotConnectedError()
        return self._cipher.decrypt(credential.access_token)

    async def _workspace(self, token: str, user: UserRead) -> GithubRepo | None:
        """The user's workspace repo if it exists and is safe to use, else None. Checked once per `VerifiedRepos`
        expiry; in between, writes go straight to it."""
        cached = self._verified.get(user)
        if cached is not None:
            return cached
        repo = await self._github.get_repo(token, f"{user.login}/{WORKSPACE_REPO}")
        if repo is None:
            return None
        if not repo.private:
            raise WorkspaceRepoPublicError()
        if await self._github.get_file(token, repo.full_name, MARKER_PATH) is None:
            raise WorkspaceRepoTakenError()
        self._verified.add(user, repo)
        return repo

    async def _create_workspace(self, token: str, user: UserRead) -> GithubRepo:
        try:
            repo = await self._github.create_private_repo(token, WORKSPACE_REPO, WORKSPACE_DESCRIPTION)
        except GithubRepoExistsError:
            # Another tab created it a moment ago, or it isn't ours: check it like any existing repo.
            existing = await self._workspace(token, user)
            if existing is None:
                raise
            return existing
        # A new repo can take a moment to accept writes.
        for delay in (*self._retry_delays, None):
            try:
                await self._github.put_file(
                    token, repo.full_name, MARKER_PATH, MARKER_CONTENT, message="Set up makable workspace", sha=None
                )
                break
            except GithubNotFoundError:
                if delay is None:
                    raise
                await self._sleep(delay)
        logger.info("workspace repo created", extra={"user_id": str(user.id)})
        self._verified.add(user, repo)
        return repo

    async def _read(self, token: str, repo: GithubRepo, path: str) -> WorkspaceSession | None:
        file = await self._github.get_file(token, repo.full_name, path)
        if file is None:
            return None
        try:
            snapshot = SessionSnapshot.model_validate(json.loads(file.content))
        except ValueError, ValidationError:
            # Edited by hand into something invalid: treat it as missing rather than failing every load.
            logger.warning("unreadable workspace session", extra={"path": path})
            return None
        return WorkspaceSession(snapshot=snapshot, sha=file.sha)


def _serialize(snapshot: SessionSnapshot) -> str:
    # exclude_unset keeps explicit nulls (a draft with no portfolio yet) and leaves out fields the SPA omitted.
    return json.dumps(snapshot.model_dump(mode="json", by_alias=True, exclude_unset=True), indent=2) + "\n"
