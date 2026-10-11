import asyncio
import hashlib
import json
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from datetime import datetime
from uuid import UUID

from fastapi import status
from pydantic import ValidationError

from app.clients.github import GithubClient, GithubConflictError, GithubNotFoundError, GithubRepoExistsError
from app.constants.workspace import (
    LATEST_FILE,
    MARKER_CONTENT,
    MARKER_PATH,
    MAX_PROJECTS,
    NEW_REPO_RETRY_DELAYS,
    PROJECTS_DIR,
    SAVE_DEADLINE_SECONDS,
    SNAPSHOT_READS_AT_ONCE,
    STATE_FILE,
    VERIFIED_REPO_TTL,
    WORKSPACE_DESCRIPTION,
    WORKSPACE_REPO,
    project_dir,
)
from app.core.exceptions import AppError, ConflictError, NotFoundError
from app.core.security import TokenCipher
from app.repositories.github_credential import GithubCredentialRepository
from app.schemas.github import GithubFile, GithubRepo, GithubTreeChange
from app.schemas.user import UserRead
from app.schemas.workspace import (
    WorkspacePartRead,
    WorkspaceSave,
    WorkspaceSaved,
    WorkspaceSnapshot,
    WorkspaceState,
    WorkspaceStateRead,
    check_part_path,
)

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


class WorkspacePartNotFoundError(NotFoundError):
    code = "workspace_part_not_found"
    message = "That part of the session isn't saved"


class WorkspaceInvalidError(AppError):
    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    code = "workspace_invalid"
    message = "That session doesn't belong here"


class WorkspaceSaveTimeoutError(AppError):
    status_code = status.HTTP_504_GATEWAY_TIMEOUT
    code = "github_timeout"
    message = "GitHub is slow right now, so this save didn't go through. Nothing was changed; try again in a moment."


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
    """Keeps the user's builder sessions in a private `makable-workspace` repo on their GitHub account, one folder
    per site (`projects/<projectId>/`). A session is split into files (see `constants/workspace.py`), so a save
    writes only what changed, as one commit. The browser keeps the working copy; nothing is stored here.

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
        save_deadline: float = SAVE_DEADLINE_SECONDS,
    ) -> None:
        self._github = github
        self._credentials = credentials
        self._cipher = cipher
        self._sleep = sleep
        self._retry_delays = retry_delays
        self._verified = verified
        self._save_deadline = save_deadline

    async def get_state(self, user: UserRead, project_id: UUID) -> WorkspaceStateRead:
        """A site's saved state (its chat chunks, AI history and files are read with `get_part`).

        Raises:
            WorkspaceSessionNotFoundError: Nothing is saved for that site (or there's no workspace repo yet).
            WorkspaceRepoTakenError: The repo exists but makable didn't create it.
            WorkspaceRepoPublicError: The repo is public.
            GithubNotConnectedError: No usable GitHub token.
            GithubError: GitHub failed.
        """
        token = await self._token(user)
        repo = await self._workspace(token, user)
        state = await self._read_state(token, repo, str(project_id)) if repo else None
        if state is None:
            raise WorkspaceSessionNotFoundError()
        return state

    async def get_latest_state(self, user: UserRead) -> WorkspaceStateRead:
        """The most recently saved state of any of the user's sites, to restore on a new device.

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
        latest = _latest_project(await self._github.get_file(token, repo.full_name, LATEST_FILE))
        state = await self._read_state(token, repo, latest) if latest else None
        if state is not None:
            return state
        # No pointer (or it was edited by hand): compare the saved states themselves.
        entries = await self._github.list_dir(token, repo.full_name, PROJECTS_DIR)
        folders = [e.name for e in entries if e.type == "dir"][:MAX_PROJECTS]
        states = await asyncio.gather(*(self._read_state(token, repo, folder) for folder in folders))
        found = [s for s in states if s is not None]
        if not found:
            raise WorkspaceSessionNotFoundError()
        return max(found, key=lambda s: datetime.fromisoformat(s.state.saved_at))

    async def get_part(self, user: UserRead, project_id: UUID, path: str) -> WorkspacePartRead:
        """One file of a saved session: a message chunk, a template's AI history or an AI-edited file.

        Raises:
            WorkspaceInvalidError: `path` isn't a session file.
            WorkspacePartNotFoundError: No such file is saved.
            GithubNotConnectedError: No usable GitHub token.
            GithubError: GitHub failed.
        """
        try:
            path = check_part_path(path)
        except ValueError:
            raise WorkspaceInvalidError("Not a session file") from None
        token = await self._token(user)
        repo = await self._workspace(token, user)
        full_path = f"{project_dir(str(project_id))}/{path}"
        file = await self._github.get_file(token, repo.full_name, full_path) if repo else None
        if file is None:
            raise WorkspacePartNotFoundError()
        return WorkspacePartRead(path=path, content=file.content)

    async def read_snapshot(
        self, user: UserRead, project_id: UUID, state_sha: str, *, max_files: int
    ) -> WorkspaceSnapshot:
        """A site's saved state and the AI-edited files of its current template, exactly as saved with
        `state_sha` (the files are read from that commit's tree, so they belong to that state). Publishing builds
        the site from this, so the browser never sends it again.

        Raises:
            WorkspaceSessionNotFoundError: Nothing is saved for that site.
            WorkspaceConflictError: The saved state isn't `state_sha` any more (another device saved since).
            WorkspaceInvalidError: The saved state can't be read, or lists more than `max_files` files.
            WorkspacePartNotFoundError: A file the state lists isn't saved.
            WorkspaceRepoTakenError: The repo exists but makable didn't create it.
            WorkspaceRepoPublicError: The repo is public.
            GithubNotConnectedError: No usable GitHub token.
            GithubError: GitHub failed.
        """
        token = await self._token(user)
        repo = await self._workspace(token, user)
        if repo is None:
            raise WorkspaceSessionNotFoundError()
        try:
            head = await self._github.get_branch_head(token, repo.full_name, repo.default_branch)
        except GithubNotFoundError:
            self._verified.discard(user)
            raise WorkspaceSessionNotFoundError() from None
        tree = await self._github.get_tree(token, repo.full_name, head.tree_sha)
        folder = project_dir(str(project_id))
        current = tree.get(f"{folder}/{STATE_FILE}")
        if current is None:
            raise WorkspaceSessionNotFoundError()
        if current != state_sha:
            raise WorkspaceConflictError(details={"sha": current})
        content = await self._github.get_blob(token, repo.full_name, current)
        state = _parse_state(GithubFile(content=content, sha=current))
        if state is None or state.project_id != project_id:
            raise WorkspaceInvalidError("The saved session can't be read")
        portfolio = state.conversation.portfolio
        template = portfolio.template if portfolio else None
        paths = state.files.get(template, []) if template else []
        if len(paths) > max_files:
            raise WorkspaceInvalidError(f"The site has more than {max_files} AI-edited files")
        shas: dict[str, str] = {}
        for path in paths:
            sha = tree.get(f"{folder}/files/{template}/{path}")
            if sha is None:
                raise WorkspacePartNotFoundError(f"The saved session is missing {path}. Save it again.")
            shas[path] = sha
        limit = asyncio.Semaphore(SNAPSHOT_READS_AT_ONCE)

        async def read(path: str, sha: str) -> tuple[str, str]:
            async with limit:
                return path, await self._github.get_blob(token, repo.full_name, sha)

        files = await asyncio.gather(*(read(path, sha) for path, sha in shas.items()))
        return WorkspaceSnapshot(state=state, files=dict(files))

    async def save(self, user: UserRead, project_id: UUID, save: WorkspaceSave) -> WorkspaceSaved:
        """Saves the changed files of a site's session and its new state as one commit, creating the private
        workspace repo on first use. Nothing is written if the state changed on GitHub since `save.base_sha`.

        Raises:
            WorkspaceInvalidError: The state is for another site than `project_id`.
            WorkspaceConflictError: Another device saved this site since `save.base_sha`.
            WorkspaceRepoTakenError: The repo exists but makable didn't create it.
            WorkspaceRepoPublicError: The repo is public.
            GithubNotConnectedError: No usable GitHub token.
            WorkspaceSaveTimeoutError: GitHub was too slow to get to the commit within `SAVE_DEADLINE_SECONDS`.
            GithubError: GitHub failed.
        """
        if save.state is not None and save.state.project_id != project_id:
            raise WorkspaceInvalidError()
        folder = project_dir(str(project_id))
        state_content = _serialize(save.state) if save.state else None
        changes = [
            *(GithubTreeChange(path=f"{folder}/{p.path}", content=p.content) for p in save.parts),
            *(GithubTreeChange(path=f"{folder}/{p}", content=None) for p in save.deletes),
        ]
        if save.state is not None and state_content is not None:
            latest = {"projectId": str(project_id), "savedAt": save.state.saved_at}
            changes += [
                GithubTreeChange(path=f"{folder}/{STATE_FILE}", content=state_content),
                GithubTreeChange(path=LATEST_FILE, content=json.dumps(latest, indent=2) + "\n"),
            ]
        if not changes:
            raise WorkspaceInvalidError("Nothing to save")
        message = f"Save session ({len(save.parts)} changed, {len(save.deletes)} removed)"
        # Everything before the commit shares one deadline; the commit runs to the end (see SAVE_DEADLINE_SECONDS).
        deadline = asyncio.get_running_loop().time() + self._save_deadline
        async with _before(deadline):
            token = await self._token(user)
            repo = await self._workspace(token, user) or await self._create_workspace(token, user)
        # Two tries: the branch can move between reading it and committing (another site's save got in first).
        for attempt in range(2):
            async with _before(deadline):
                try:
                    head = await self._github.get_branch_head(token, repo.full_name, repo.default_branch)
                except GithubNotFoundError:
                    # The repo checked earlier is gone (deleted or renamed on GitHub): check it again.
                    self._verified.discard(user)
                    repo = await self._workspace(token, user) or await self._create_workspace(token, user)
                    head = await self._github.get_branch_head(token, repo.full_name, repo.default_branch)
                current = await self._github.get_file(token, repo.full_name, f"{folder}/{STATE_FILE}")
            if (current.sha if current else None) != save.base_sha:
                raise WorkspaceConflictError(details=_conflict_details(current))
            try:
                await self._github.commit_changes(
                    token, repo.full_name, repo.default_branch, head, changes, message=message
                )
                break
            except GithubConflictError:
                if attempt == 1:
                    raise WorkspaceConflictError() from None
        logger.info("workspace session saved", extra={"user_id": str(user.id), "project_id": str(project_id)})
        return WorkspaceSaved(
            sha=git_blob_sha(state_content) if state_content else save.base_sha, repo_url=repo.html_url
        )

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
            if not await self._unfinished_setup(token, repo):
                raise WorkspaceRepoTakenError()
            await self._write_marker(token, repo)
        self._verified.add(user, repo)
        return repo

    async def _unfinished_setup(self, token: str, repo: GithubRepo) -> bool:
        """Whether a repo without the marker is one makable created whose setup didn't finish (the marker write
        failed, or another tab is still writing it): makable's description and nothing but the README GitHub
        adds. Taking it over writes nothing a person made."""
        if repo.description != WORKSPACE_DESCRIPTION:
            return False
        entries = await self._github.list_dir(token, repo.full_name, "")
        return [e.name for e in entries] == ["README.md"]

    async def _create_workspace(self, token: str, user: UserRead) -> GithubRepo:
        try:
            repo = await self._github.create_repo(token, WORKSPACE_REPO, WORKSPACE_DESCRIPTION, private=True)
        except GithubRepoExistsError:
            # Another tab created it a moment ago, or it isn't ours: check it like any existing repo.
            existing = await self._workspace(token, user)
            if existing is None:
                raise
            return existing
        await self._write_marker(token, repo)
        logger.info("workspace repo created", extra={"user_id": str(user.id)})
        self._verified.add(user, repo)
        return repo

    async def _write_marker(self, token: str, repo: GithubRepo) -> None:
        # A new repo can take a moment to accept writes.
        for delay in (*self._retry_delays, None):
            try:
                await self._github.put_file(
                    token, repo.full_name, MARKER_PATH, MARKER_CONTENT, message="Set up makable workspace", sha=None
                )
                return
            except GithubConflictError:
                # Written meanwhile: fine if it's makable's marker (another tab finished the setup).
                marker = await self._github.get_file(token, repo.full_name, MARKER_PATH)
                if marker is None or marker.content != MARKER_CONTENT:
                    raise WorkspaceRepoTakenError() from None
                return
            except GithubNotFoundError:
                if delay is None:
                    raise
                await self._sleep(delay)

    async def _read_state(self, token: str, repo: GithubRepo, project_id: str) -> WorkspaceStateRead | None:
        file = await self._github.get_file(token, repo.full_name, f"{project_dir(project_id)}/{STATE_FILE}")
        state = _parse_state(file)
        return WorkspaceStateRead(state=state, sha=file.sha) if file and state else None


@asynccontextmanager
async def _before(deadline: float) -> AsyncIterator[None]:
    """Runs the block until `deadline` (event-loop time), then gives up with `WorkspaceSaveTimeoutError`."""
    try:
        async with asyncio.timeout_at(deadline):
            yield
    except TimeoutError:
        raise WorkspaceSaveTimeoutError() from None


def _parse_state(file: GithubFile | None) -> WorkspaceState | None:
    if file is None:
        return None
    try:
        return WorkspaceState.model_validate(json.loads(file.content))
    except ValueError, ValidationError:
        # Edited by hand into something invalid: treat it as missing rather than failing every load.
        logger.warning("unreadable workspace state")
        return None


def _latest_project(file: GithubFile | None) -> str | None:
    """The project id `LATEST_FILE` points to, if it's readable."""
    try:
        data = json.loads(file.content) if file else None
        return str(UUID(data["projectId"])) if isinstance(data, dict) else None
    except ValueError, KeyError, TypeError:
        return None


def _conflict_details(current: GithubFile | None) -> dict[str, str] | None:
    state = _parse_state(current)
    if current is None:
        return None
    return {"sha": current.sha, **({"savedAt": state.saved_at} if state else {})}


def _serialize(state: WorkspaceState) -> str:
    # exclude_unset keeps explicit nulls (a draft with no portfolio yet) and leaves out fields the SPA omitted.
    return json.dumps(state.model_dump(mode="json", by_alias=True, exclude_unset=True), indent=2) + "\n"


def git_blob_sha(content: str | bytes) -> str:
    """The SHA git (and so GitHub) gives a file with this content: what the next save must send back."""
    data = content.encode() if isinstance(content, str) else content
    return hashlib.sha1(b"blob %d\0" % len(data) + data, usedforsecurity=False).hexdigest()
