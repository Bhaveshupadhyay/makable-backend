import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from uuid import UUID

from fastapi import status

from app.agents.create_site.state import NameTakenError
from app.clients.github import GithubClient, GithubConflictError, GithubNotFoundError, GithubRepoExistsError
from app.constants.site import (
    MANIFEST_FORMAT,
    MANIFEST_PATH,
    NEW_REPO_RETRY_DELAYS,
    RESERVED_PATH,
    SAVE_DEADLINE_SECONDS,
    SITE_DESCRIPTION,
    VERIFIED_SITE_TTL,
)
from app.core.exceptions import AppError, ConflictError, NotFoundError
from app.core.security import TokenCipher
from app.models.site import Site
from app.repositories.github_credential import GithubCredentialRepository
from app.repositories.site import SiteRepository
from app.repositories.unit_of_work import UnitOfWork
from app.schemas.github import GithubBranchHead, GithubRepo, GithubTreeChange
from app.schemas.site import (
    SiteCreate,
    SiteFiles,
    SiteNew,
    SiteRead,
    SiteSave,
    SiteSaved,
    SiteSnapshot,
    SiteUndo,
    SiteUndone,
)
from app.schemas.user import UserRead
from app.services.site_snapshots import SITE_SNAPSHOTS, SiteSnapshots, snapshot_from_archive
from app.utils.git import git_blob_sha
from app.utils.site_scaffold import repo_name_for, scaffold_files

logger = logging.getLogger(__name__)


class SiteNotFoundError(NotFoundError):
    code = "site_not_found"
    message = "This site wasn't found."


class SiteNotReadyError(ConflictError):
    code = "site_not_ready"
    message = "This site has no repository on GitHub yet."


class SiteRepoTakenError(ConflictError):
    """`details["repoName"]` is the name that's taken."""

    code = "site_repo_taken"
    message = "You already have a repository with that name. Pick another name for this site."


class SiteRepoMissingError(NotFoundError):
    code = "site_repo_missing"
    message = "This site's repository isn't on your GitHub any more."


class SiteRepoChangedError(ConflictError):
    code = "site_repo_changed"
    message = "This site's repository on GitHub no longer has makable's manifest for it, so makable won't change it."


class SiteConflictError(ConflictError):
    """`details["commitSha"]` is the commit the site is at on GitHub now."""

    code = "site_conflict"
    message = "This site was changed somewhere else (another tab or device) since it was loaded here."


class SiteUndoConflictError(ConflictError):
    code = "site_undo_conflict"
    message = "That change can't be undone: the files it changed were changed again since."


class SiteInvalidError(AppError):
    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    code = "site_invalid"
    message = "This change can't be made to the site."


class SiteGithubNotConnectedError(AppError):
    status_code = status.HTTP_401_UNAUTHORIZED
    code = "github_reconnect"
    message = "Connect GitHub again to work on your site."


class SiteSaveTimeoutError(AppError):
    status_code = status.HTTP_504_GATEWAY_TIMEOUT
    code = "github_timeout"
    message = "GitHub is slow right now. Your changes weren't saved yet; they'll be saved again in a moment."


class VerifiedSites:
    """Site repos checked recently (they exist and their manifest names the site), so a save skips those requests.
    Process-wide: each worker keeps its own. Entries expire after `ttl` seconds."""

    MAX_ENTRIES = 10_000

    def __init__(self, ttl: float = VERIFIED_SITE_TTL, clock: Callable[[], float] = time.monotonic) -> None:
        self._ttl = ttl
        self._clock = clock
        self._repos: dict[tuple[UUID, str], tuple[GithubRepo, float]] = {}

    def get(self, site_id: UUID, full_name: str) -> GithubRepo | None:
        entry = self._repos.get((site_id, full_name))
        if entry is None or entry[1] <= self._clock():
            self._repos.pop((site_id, full_name), None)
            return None
        return entry[0]

    def add(self, site_id: UUID, repo: GithubRepo) -> None:
        now = self._clock()
        if len(self._repos) >= self.MAX_ENTRIES:
            self._repos = {key: entry for key, entry in self._repos.items() if entry[1] > now}
        self._repos[(site_id, repo.full_name)] = (repo, now + self._ttl)

    def discard(self, site_id: UUID, full_name: str) -> None:
        self._repos.pop((site_id, full_name), None)

    def clear(self) -> None:
        self._repos.clear()


VERIFIED_SITES = VerifiedSites()


class SiteAccess:
    """One site's repo, with the user's token: what the AI agent reads the site from and commits to. It never sees
    the token or the database."""

    def __init__(self, service: SiteService, token: str, repo: GithubRepo, site_id: UUID) -> None:
        self._service = service
        self._token = token
        self.repo = repo
        self.site_id = site_id

    async def head(self) -> str:
        """The commit the site's branch is at."""
        return (await self._service.branch_head(self._token, self.repo)).commit_sha

    async def snapshot(self, commit_sha: str) -> SiteSnapshot:
        """The site's files at a commit (cached).

        Raises:
            SiteNotFoundError: No such commit in the site's repo.
        """
        return await self._service.snapshot(self._token, self.repo, commit_sha)

    async def commit(self, base_commit: str, changes: dict[str, str | None], message: str) -> str:
        """Writes `changes` (None deletes a file) as one commit on top of `base_commit`. Returns the new commit.

        Raises:
            SiteConflictError: The site isn't at `base_commit` on GitHub any more.
        """
        return await self._service.commit(self._token, self.repo, base_commit, changes, message)


class SiteCreatorAccess:
    """A site's repo for the create agent (`SiteCreator`): `create` makes it with the starter files; then it reads and
    commits like `SiteAccess`. A taken name is the agent's `NameTakenError`, so it can ask for another."""

    def __init__(self, service: SiteService, user: UserRead, project_id: UUID) -> None:
        self._service = service
        self._user = user
        self._project_id = project_id
        self._access: SiteAccess | None = None

    async def create(self, name: str) -> str:
        try:
            created = await self._service.create(self._user, self._project_id, SiteNew(name=name))
        except SiteRepoTakenError as err:
            raise NameTakenError(str((err.details or {}).get("repoName", name))) from None
        self._access = None
        return created.commit_sha

    async def snapshot(self, commit_sha: str) -> SiteSnapshot:
        return await (await self._site()).snapshot(commit_sha)

    async def commit(self, base_commit: str, changes: dict[str, str | None], message: str) -> str:
        return await (await self._site()).commit(base_commit, changes, message)

    async def _site(self) -> SiteAccess:
        if self._access is None:
            self._access = await self._service.access(self._user, self._project_id)
        return self._access


class SiteService:
    """Sites: a row for whose site it is, and the code in a private repo on the user's GitHub (one per site, marked
    with `.makable/manifest.json`). makable reads a site as an archive at a commit and writes every change as a
    commit: the user's manual edits (`save`), AI edits (through `SiteAccess`) and Undo. The browser keeps a working
    copy at a commit, and each write names the commit it builds on, so a change made elsewhere is never overwritten.
    """

    def __init__(
        self,
        github: GithubClient,
        sites: SiteRepository,
        credentials: GithubCredentialRepository,
        cipher: TokenCipher,
        uow: UnitOfWork,
        *,
        snapshots: SiteSnapshots = SITE_SNAPSHOTS,
        verified: VerifiedSites = VERIFIED_SITES,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        retry_delays: Sequence[float] = NEW_REPO_RETRY_DELAYS,
        save_deadline: float = SAVE_DEADLINE_SECONDS,
    ) -> None:
        self._github = github
        self._sites = sites
        self._credentials = credentials
        self._cipher = cipher
        self._uow = uow
        self._snapshots = snapshots
        self._verified = verified
        self._sleep = sleep
        self._retry_delays = retry_delays
        self._save_deadline = save_deadline

    async def list_sites(self, user: UserRead) -> list[SiteRead]:
        """The user's sites, the most recently used first."""
        return [self._read(user, site) for site in await self._sites.list_for_user(user.id)]

    async def read(self, user: UserRead, project_id: UUID) -> SiteRead:
        """The site's row as the SPA sees it.

        Raises:
            SiteNotFoundError: No such site, or it's another user's.
        """
        site = await self._sites.get(project_id)
        if site is None or site.user_id != user.id:
            raise SiteNotFoundError()
        return self._read(user, site)

    async def create(self, user: UserRead, project_id: UUID, data: SiteNew) -> SiteFiles:
        """Creates the site's private repo, named after the site, with makable's starter files. A site that already
        has its repo just loads (a retried request). A repo left by a create that didn't finish (it has this site's
        manifest, or only GitHub's README and makable's description) is taken over.

        Raises:
            SiteNotFoundError: The site is another user's.
            SiteRepoTakenError: The user has a repo with that name that isn't this site's, or another of their sites
                has it (even if it's gone from GitHub).
            SiteGithubNotConnectedError: No usable GitHub token.
            GithubError: GitHub failed.
        """
        site = await self._sites.get(project_id)
        if site is not None and site.user_id != user.id:
            raise SiteNotFoundError()
        if site is not None and site.repo_name:
            return await self.load(user, project_id)
        repo_name = repo_name_for(data.name)
        if await self._sites.get_by_repo(user.id, repo_name) is not None:
            raise SiteRepoTakenError(details={"repoName": repo_name})
        token = await self._token(user)
        full_name = f"{user.login}/{repo_name}"
        existing = await self._github.get_repo(token, full_name)
        fresh = existing is None
        if existing is not None and await self._manifest_project(token, existing) != project_id:
            if not await self._unfinished_create(token, existing):
                raise SiteRepoTakenError(details={"repoName": repo_name})
            fresh = True
        if existing is None:
            try:
                existing = await self._github.create_repo(token, repo_name, SITE_DESCRIPTION, private=True)
            except GithubRepoExistsError:
                raise SiteRepoTakenError(details={"repoName": repo_name}) from None
            logger.info("site repo created", extra={"user_id": str(user.id), "repo": existing.full_name})
        repo = existing
        head = await self._new_repo_head(token, repo)
        tree = await self._github.get_tree(token, repo.full_name, head.tree_sha)
        # A repo made just now gets every starter file (over GitHub's README). One taken over keeps what it has.
        changes = [
            GithubTreeChange(path=path, content=content)
            for path, content in scaffold_files(data.name, project_id).items()
            if (fresh and tree.get(path) != git_blob_sha(content)) or path not in tree
        ]
        commit_sha = head.commit_sha
        if changes:
            commit_sha = await self._github.commit_changes(
                token, repo.full_name, repo.default_branch, head, changes, message=f"Start {data.name} with makable"
            )
        if site is None:
            site = await self._sites.create(
                SiteCreate(id=project_id, user_id=user.id, name=data.name, repo_name=repo.name)
            )
        else:
            await self._sites.set_repo(project_id, repo.name, data.name)
            site.name, site.repo_name = data.name, repo.name
        await self._uow.commit()
        self._verified.add(project_id, repo)
        snapshot = await self.snapshot(token, repo, commit_sha)
        return SiteFiles(site=self._read(user, site, repo.name), **snapshot.model_dump())

    async def load(self, user: UserRead, project_id: UUID) -> SiteFiles:
        """The site's files on GitHub now.

        Raises:
            SiteNotFoundError: No such site, or it's another user's.
            SiteNotReadyError: The site has no repo yet.
            SiteRepoMissingError: The repo was deleted or renamed on GitHub.
            SiteRepoChangedError: The repo's manifest is gone or names another site.
            SiteGithubNotConnectedError: No usable GitHub token.
            GithubError: GitHub failed.
        """
        site, token, repo = await self._site_repo(user, project_id)
        # Read before the transaction ends: that expires the row.
        read = self._read(user, site)
        await self._uow.rollback()
        head = await self.branch_head(token, repo)
        snapshot = await self.snapshot(token, repo, head.commit_sha)
        return SiteFiles(site=read, **snapshot.model_dump())

    async def github_repo(self, user: UserRead, project_id: UUID) -> tuple[str, GithubRepo]:
        """The user's GitHub token and the site's repo (checked), for services that call GitHub about the repo
        itself (publishing). Ends the request's transaction.

        Raises:
            The errors of `access`.
        """
        _, token, repo = await self._site_repo(user, project_id)
        await self._uow.rollback()
        return token, repo

    def forget_repo(self, project_id: UUID, full_name: str) -> None:
        """Drops a repo from the verified cache (e.g. its visibility changed)."""
        self._verified.discard(project_id, full_name)

    async def access(self, user: UserRead, project_id: UUID) -> SiteAccess:
        """The site's repo, checked, for the AI agent. Ends the request's transaction: what follows is slow.

        Raises:
            SiteNotFoundError: No such site, or it's another user's.
            SiteNotReadyError: The site has no repo yet.
            SiteRepoMissingError: The repo was deleted or renamed on GitHub.
            SiteRepoChangedError: The repo's manifest is gone or names another site.
            SiteGithubNotConnectedError: No usable GitHub token.
            GithubError: GitHub failed.
        """
        _, token, repo = await self._site_repo(user, project_id)
        await self._uow.rollback()
        return SiteAccess(self, token, repo, project_id)

    async def ensure(self, user: UserRead, project_id: UUID, name: str) -> SiteRead:
        """The user's site, made (without a repo yet) if there's none: what its AI threads belong to.

        Raises:
            SiteNotFoundError: The site is another user's.
        """
        site = await self._sites.get(project_id)
        if site is not None and site.user_id != user.id:
            raise SiteNotFoundError()
        if site is None:
            site = await self._sites.create(SiteCreate(id=project_id, user_id=user.id, name=name))
            await self._uow.commit()
        return self._read(user, site)

    def creator(self, user: UserRead, project_id: UUID) -> SiteCreatorAccess:
        """The site's repo for the create agent: it makes the repo, then reads and commits like `access`."""
        return SiteCreatorAccess(self, user, project_id)

    async def save(self, user: UserRead, project_id: UUID, data: SiteSave) -> SiteSaved:
        """Commits the user's manual edits on top of `base_commit`: only files that differ from it (none: no commit).

        Raises:
            SiteNotFoundError: No such site, or it's another user's.
            SiteNotReadyError: The site has no repo yet.
            SiteConflictError: The site isn't at `base_commit` on GitHub any more.
            SiteInvalidError: A file is both written and deleted.
            SiteRepoMissingError: The repo was deleted or renamed on GitHub.
            SiteRepoChangedError: The repo's manifest is gone or names another site.
            SiteSaveTimeoutError: GitHub was too slow to get to the commit.
            SiteGithubNotConnectedError: No usable GitHub token.
            GithubError: GitHub failed.
        """
        if set(data.files) & set(data.deletes):
            raise SiteInvalidError("A file can't be both saved and deleted.")
        _, token, repo = await self._site_repo(user, project_id)
        await self._uow.rollback()
        try:
            async with asyncio.timeout(self._save_deadline):
                head = await self.branch_head(token, repo)
                if head.commit_sha != data.base_commit:
                    raise SiteConflictError(details={"commitSha": head.commit_sha})
                tree = await self._github.get_tree(token, repo.full_name, head.tree_sha)
        except TimeoutError:
            raise SiteSaveTimeoutError() from None
        changes = [
            GithubTreeChange(path=path, content=content)
            for path, content in data.files.items()
            if tree.get(path) != git_blob_sha(content)
        ]
        changes += [GithubTreeChange(path=path, content=None) for path in data.deletes if path in tree]
        if not changes:
            return SiteSaved(commit_sha=head.commit_sha, changed=False)
        message = data.message or _manual_message(changes)
        commit_sha = await self._commit_on(token, repo, head, changes, message)
        logger.info("site saved", extra={"user_id": str(user.id), "project_id": str(project_id), "files": len(changes)})
        return SiteSaved(commit_sha=commit_sha, changed=True)

    async def undo(self, user: UserRead, project_id: UUID, data: SiteUndo) -> SiteUndone:
        """Undoes a commit's changes (e.g. an AI edit) with a new commit on top of `base_commit`: each file it changed
        goes back to what it was before it, as long as nothing changed that file since.

        Raises:
            SiteNotFoundError: No such site or commit, or the site is another user's.
            SiteNotReadyError: The site has no repo yet.
            SiteConflictError: The site isn't at `base_commit` on GitHub any more.
            SiteUndoConflictError: A file the commit changed was changed again since.
            SiteInvalidError: The commit is the repo's first.
            SiteRepoMissingError: The repo was deleted or renamed on GitHub.
            SiteRepoChangedError: The repo's manifest is gone or names another site.
            SiteGithubNotConnectedError: No usable GitHub token.
            GithubError: GitHub failed.
        """
        _, token, repo = await self._site_repo(user, project_id)
        await self._uow.rollback()
        try:
            commit = await self._github.get_commit(token, repo.full_name, data.commit_sha)
        except GithubNotFoundError:
            raise SiteNotFoundError("That change wasn't found on GitHub.") from None
        if not commit.parents:
            raise SiteInvalidError("The site's first version can't be undone.")
        before, after, base = await asyncio.gather(
            self.snapshot(token, repo, commit.parents[0]),
            self.snapshot(token, repo, commit.sha),
            self.snapshot(token, repo, data.base_commit),
        )
        changed = sorted(
            path
            for path in set(before.files) | set(after.files)
            if before.files.get(path) != after.files.get(path) and not RESERVED_PATH.match(path)
        )
        if any(base.files.get(path) != after.files.get(path) for path in changed):
            raise SiteUndoConflictError()
        restored = {path: before.files.get(path) for path in changed}
        if not restored:
            return SiteUndone(commit_sha=data.base_commit, files={})
        title = commit.message.splitlines()[0] if commit.message else "a change"
        commit_sha = await self.commit(token, repo, data.base_commit, restored, f"Undo: {title}"[:200])
        return SiteUndone(commit_sha=commit_sha, files=restored)

    async def branch_head(self, token: str, repo: GithubRepo) -> GithubBranchHead:
        """Where the site's branch is.

        Raises:
            SiteRepoMissingError: The repo or its branch is gone.
        """
        try:
            return await self._github.get_branch_head(token, repo.full_name, repo.default_branch)
        except GithubNotFoundError:
            raise SiteRepoMissingError() from None

    async def snapshot(self, token: str, repo: GithubRepo, commit_sha: str) -> SiteSnapshot:
        """The site's files at a commit, from the cache or one archive download.

        Raises:
            SiteNotFoundError: No such commit in the repo.
        """

        async def download() -> SiteSnapshot:
            try:
                archive = await self._github.get_archive(token, repo.full_name, commit_sha)
            except GithubNotFoundError:
                raise SiteNotFoundError("That version of the site wasn't found on GitHub.") from None
            return snapshot_from_archive(commit_sha, archive)

        return await self._snapshots.get(repo.full_name, commit_sha, download)

    async def commit(
        self,
        token: str,
        repo: GithubRepo,
        base_commit: str,
        changes: dict[str, str | None],
        message: str,
        *,
        own_files: bool = False,
    ) -> str:
        """Writes `changes` as one commit on top of `base_commit`, which must be where the branch is. Only makable's
        own writes (`own_files`: the deploy workflow, the manifest) may touch `.github/` or `.makable/`.

        Raises:
            SiteConflictError: The branch isn't at `base_commit`.
            SiteInvalidError: A change touches makable's or git's own files.
        """
        if not own_files and any(RESERVED_PATH.match(path) for path in changes):
            raise SiteInvalidError("makable's own files can't be changed like this.")
        head = await self.branch_head(token, repo)
        if head.commit_sha != base_commit:
            raise SiteConflictError(details={"commitSha": head.commit_sha})
        tree_changes = [GithubTreeChange(path=path, content=content) for path, content in changes.items()]
        return await self._commit_on(token, repo, head, tree_changes, message)

    async def _commit_on(
        self, token: str, repo: GithubRepo, head: GithubBranchHead, changes: list[GithubTreeChange], message: str
    ) -> str:
        try:
            commit_sha = await self._github.commit_changes(
                token, repo.full_name, repo.default_branch, head, changes, message=message
            )
        except GithubConflictError:
            raise SiteConflictError(details={"commitSha": (await self.branch_head(token, repo)).commit_sha}) from None
        # The new commit's files are the base's with the changes, when the base is cached: save the download.
        base = self._snapshots.peek(repo.full_name, head.commit_sha)
        if base is not None:
            files = dict(base.files)
            for change in changes:
                if change.content is None:
                    files.pop(change.path, None)
                elif isinstance(change.content, str):
                    files[change.path] = change.content
            self._snapshots.put(repo.full_name, SiteSnapshot(commit_sha=commit_sha, files=files, binary=base.binary))
        return commit_sha

    async def _site_repo(self, user: UserRead, project_id: UUID) -> tuple[Site, str, GithubRepo]:
        """The user's site, their token and the site's repo, checked."""
        site = await self._sites.get(project_id)
        if site is None or site.user_id != user.id:
            raise SiteNotFoundError()
        if not site.repo_name:
            raise SiteNotReadyError()
        token = await self._token(user)
        repo = await self._checked_repo(token, project_id, f"{user.login}/{site.repo_name}")
        return site, token, repo

    async def _checked_repo(self, token: str, site_id: UUID, full_name: str) -> GithubRepo:
        repo = self._verified.get(site_id, full_name)
        if repo is not None:
            return repo
        repo = await self._github.get_repo(token, full_name)
        if repo is None:
            raise SiteRepoMissingError()
        if await self._manifest_project(token, repo) != site_id:
            raise SiteRepoChangedError()
        self._verified.add(site_id, repo)
        return repo

    async def _manifest_project(self, token: str, repo: GithubRepo) -> UUID | None:
        """The site a repo's manifest names, or None (no manifest, or not one of makable's)."""
        file = await self._github.get_file(token, repo.full_name, MANIFEST_PATH)
        if file is None:
            return None
        try:
            manifest = json.loads(file.content)
            if manifest.get("format") != MANIFEST_FORMAT:
                return None
            return UUID(str(manifest["projectId"]))
        except ValueError, KeyError, TypeError, AttributeError:
            return None

    async def _unfinished_create(self, token: str, repo: GithubRepo) -> bool:
        """Whether a repo without this site's manifest is one makable created for a site whose first commit didn't
        happen: makable's description and nothing but GitHub's README."""
        if repo.description != SITE_DESCRIPTION:
            return False
        entries = await self._github.list_dir(token, repo.full_name, "")
        return [e.name for e in entries] == ["README.md"]

    async def _new_repo_head(self, token: str, repo: GithubRepo) -> GithubBranchHead:
        # A new repo can take a moment to show its first commit.
        for delay in (*self._retry_delays, None):
            try:
                return await self._github.get_branch_head(token, repo.full_name, repo.default_branch)
            except GithubNotFoundError:
                if delay is None:
                    raise SiteRepoMissingError() from None
                await self._sleep(delay)
        raise AssertionError("unreachable")

    async def _token(self, user: UserRead) -> str:
        credential = await self._credentials.get(user.id)
        if credential is None:
            raise SiteGithubNotConnectedError()
        return self._cipher.decrypt(credential.access_token)

    @staticmethod
    def _read(user: UserRead, site: Site, repo_name: str | None = None) -> SiteRead:
        name = repo_name or site.repo_name
        return SiteRead(
            project_id=site.id,
            name=site.name,
            repo_name=name,
            repo_url=f"https://github.com/{user.login}/{name}" if name else None,
            updated_at=site.updated_at,
        )


def _manual_message(changes: list[GithubTreeChange]) -> str:
    if len(changes) == 1:
        verb = "Delete" if changes[0].content is None else "Edit"
        return f"{verb} {changes[0].path}"
    return f"Edit {len(changes)} files"
