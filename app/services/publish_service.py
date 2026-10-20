import asyncio
import json
import logging
import re
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime, timedelta
from uuid import UUID

from fastapi import status
from pydantic import ValidationError

from app.clients.deploy_platforms import DeployPlatformClient, PreparedProject
from app.clients.github import GithubClient, GithubNotFoundError
from app.constants.publish import (
    DEPLOY_CLOCK_SKEW_SECONDS,
    DISPATCH_RETRY_DELAYS,
    PROJECT_NAME_MAX,
    TARGET_NAMES,
    TARGET_SECRETS,
    TARGET_TOKEN_URLS,
    WORKFLOW_FILE,
    WORKFLOW_PATH,
    PublishTarget,
    SiteKind,
)
from app.constants.site import MANIFEST_PATH
from app.core.database import utc_now
from app.core.exceptions import AppError, ConflictError, NotFoundError, ServiceUnavailableError
from app.core.security import seal_for_github
from app.repositories.site_analytics import SiteAnalyticsRepository
from app.repositories.unit_of_work import UnitOfWork
from app.schemas.github import GithubRepo
from app.schemas.publish import (
    DeployRequest,
    DeployStarted,
    DeployState,
    PublishCredentials,
    PublishRequest,
    PublishResult,
    PublishSettings,
    PublishStatus,
    PublishTargetInfo,
    WorkflowRun,
)
from app.schemas.site import SiteSnapshot
from app.schemas.user import UserRead
from app.services.publish_workflows import render_workflow
from app.services.site_service import SiteConflictError, SiteService

logger = logging.getLogger(__name__)

# Branch names the workflow can use as they are.
_BRANCH = re.compile(r"[A-Za-z0-9._/-]{1,100}")


class PublishMissingScopeError(AppError):
    """The user signed in before makable asked for the `workflow` scope, which writing a workflow file needs."""

    status_code = status.HTTP_403_FORBIDDEN
    code = "github_missing_scope"
    message = "Connect GitHub again to let makable set up your site's deploy workflow."


class PublishInvalidError(AppError):
    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    code = "publish_invalid"
    message = "This site can't be published like that."


class PublishRepoArchivedError(ConflictError):
    code = "publish_repo_archived"
    message = "This site's repository is archived on GitHub. Unarchive it to publish."


class PublishPagesPrivateError(ConflictError):
    """The site's repo is private and GitHub Pages needs it public: the SPA asks, and sends `makePublic`."""

    code = "publish_pages_private"
    message = "GitHub Pages needs the site's repository to be public. Make it public, or pick another platform."


class PublishNotPublishedError(NotFoundError):
    code = "publish_not_published"
    message = "This site hasn't been published yet."


class PublishSecretsMissingError(ConflictError):
    """`details` has the `missing` secret names and the `secretsUrl` where the user adds them."""

    code = "publish_secrets_missing"
    message = "Add the deploy secrets to the repository on GitHub first."


class PublishDeployNotStartedError(ServiceUnavailableError):
    """The workflow is on GitHub, but GitHub wouldn't start it in time. `details` has the `actionsUrl`; running the
    deploy again (`POST /publish/{id}/deploy`) usually works a moment later."""

    code = "publish_deploy_not_started"
    message = "Your site is ready on GitHub, but GitHub didn't start the deploy. Try deploying again in a moment."


class PublishService:
    """Publishes a site from its repo to the platform the user picked (GitHub Pages, Vercel, Cloudflare Pages or
    Netlify): the repo gets a deploy workflow (`.github/workflows/makable-deploy.yml`) and the manifest records where
    the site is published, in one commit when either changes; then makable starts the deploy. The workflow has no push
    trigger, so a change goes live only with a publish.

    A platform token pasted in the chat is checked, used to create the platform project, and saved as an Actions
    secret (sealed so only GitHub can read it); makable stores nothing. Otherwise the user adds the secrets on GitHub,
    and the workflow skips the deploy until they're there.

    A site linked to a Google Analytics stream gets the stream's tag in its built pages (the workflow adds it), and
    the link records that it was published.
    """

    def __init__(
        self,
        github: GithubClient,
        sites: SiteService,
        platforms: DeployPlatformClient,
        analytics: SiteAnalyticsRepository,
        uow: UnitOfWork,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        dispatch_delays: Sequence[float] = DISPATCH_RETRY_DELAYS,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._github = github
        self._sites = sites
        self._platforms = platforms
        self._analytics = analytics
        self._uow = uow
        self._sleep = sleep
        self._dispatch_delays = dispatch_delays
        self._clock = clock

    def targets(self) -> list[PublishTargetInfo]:
        """The platforms a site can be published to, with the secrets each one's workflow reads."""
        return [
            PublishTargetInfo(
                id=target,
                name=TARGET_NAMES[target],
                secrets=list(TARGET_SECRETS[target]),
                token_url=TARGET_TOKEN_URLS[target],
            )
            for target in PublishTarget
        ]

    async def publish(self, user: UserRead, project_id: UUID, request: PublishRequest) -> PublishResult:
        """Commits the deploy workflow for `request.target` (and the publish settings) to the site's repo on top of
        `request.base_commit` when they change, then starts the deploy, even when nothing changed (it deploys what's
        on GitHub). With `request.credentials`, first checks the token, creates the platform project and saves the
        token as an Actions secret. Without the platform's secrets on the repo, the deploy isn't started.

        Raises:
            SiteNotFoundError / SiteNotReadyError / SiteRepoMissingError / SiteRepoChangedError: The site's repo isn't
                one makable can publish.
            SiteConflictError: The site isn't at `base_commit` on GitHub any more.
            PublishPagesPrivateError: GitHub Pages was picked for a private repo, without `make_public`.
            PublishRepoArchivedError: The repo is archived.
            PublishInvalidError: The site has neither src/main.tsx nor index.html, or its branch name can't be used.
            PublishMissingScopeError: The GitHub token can't write workflow files.
            PlatformTokenInvalidError: The platform refused the pasted token.
            PlatformAccountRequiredError: The Cloudflare token sees several accounts and none was picked.
            PlatformProjectTakenError: The platform project name belongs to someone else.
            PlatformError: The platform failed.
            GithubPagesUnavailableError: GitHub won't turn Pages on for the repo.
            PublishDeployNotStartedError: The workflow was committed, but GitHub wouldn't start it.
            GithubError: GitHub failed.
        """
        target = request.target
        pages = target == PublishTarget.GITHUB_PAGES
        token, repo = await self._sites.github_repo(user, project_id)
        measurement_id = await self._measurement_id(project_id)
        scopes = await self._github.get_scopes(token)
        if scopes is not None and "workflow" not in scopes:
            raise PublishMissingScopeError()
        if repo.archived:
            raise PublishRepoArchivedError()
        branch = _check_branch(repo.default_branch)
        head = await self._sites.branch_head(token, repo)
        if head.commit_sha != request.base_commit:
            raise SiteConflictError(details={"commitSha": head.commit_sha})
        snapshot = await self._sites.snapshot(token, repo, request.base_commit)
        kind = _site_kind(snapshot)
        if pages and repo.private and not request.make_public:
            raise PublishPagesPrivateError()

        manifest = _manifest(snapshot)
        published = _settings(manifest)
        project_name = None
        if not pages:
            same = published is not None and published.target == target
            project_name = (
                request.project_name
                or (published.project_name if same and published else None)
                or _default_project(user.login, repo.name)
            )
        prepared = await self._prepare(target, request.credentials, project_name, branch)
        site_url = _site_url(user.login, repo.name, target, project_name, prepared, published)

        workflow = render_workflow(
            target,
            kind,
            branch=branch,
            base_path=_pages_base_path(user.login, repo.name) if pages else "/",
            project_name=project_name,
            measurement_id=measurement_id,
        )
        settings = PublishSettings(target=target, project_name=project_name, site_url=site_url)
        manifest["publish"] = settings.model_dump(mode="json", by_alias=True)
        manifest_content = json.dumps(manifest, indent=2) + "\n"
        # makable owns these two files: they're written as makable makes them, also over edits made on GitHub. Only
        # Publish runs the workflow; a user's own steps go in a workflow of their own.
        changes: dict[str, str | None] = {
            path: content
            for path, content in ((WORKFLOW_PATH, workflow), (MANIFEST_PATH, manifest_content))
            if snapshot.files.get(path) != content
        }

        # Pages and the secrets come before the deploy starts, so its run can use them. A private repo is made public
        # for Pages only now, once everything that could refuse the publish has passed, and private again if Pages or
        # the commit fails. The commit is the last step that can fail, so a publish that fails has moved nothing.
        made_public = False
        try:
            if pages:
                if repo.private:
                    repo = await self._set_private(token, project_id, repo, private=False)
                    made_public = True
                await self._github.enable_pages_workflow(token, repo.full_name)
            if prepared is not None:
                await self._set_secrets(token, repo.full_name, prepared.secrets)

            commit_sha = request.base_commit
            if changes:
                message = f"Publish to {TARGET_NAMES[target]}"
                commit_sha = await self._sites.commit(
                    token, repo, request.base_commit, changes, message, own_files=True
                )
        except Exception:
            if made_public:
                await self._make_private_again(token, project_id, repo)
            raise
        if measurement_id is not None:
            # The deploy workflow on GitHub has the tag now.
            await self._analytics.set_published(project_id, measurement_id)
            await self._uow.commit()

        missing = [] if prepared else await self._missing_secrets(token, repo.full_name, target)
        deploy: DeployState = "waiting_for_secrets" if missing else "started"
        started_at = None if missing else await self._start_deploy(token, repo, branch)
        logger.info(
            "site published",
            extra={
                "user_id": str(user.id),
                "project_id": str(project_id),
                "target": target,
                "changed": bool(changes),
                "deploy": deploy,
            },
        )
        return PublishResult(
            target=target,
            repo_full_name=repo.full_name,
            repo_url=repo.html_url,
            branch=branch,
            commit_sha=commit_sha,
            changed=bool(changes),
            deploy=deploy,
            deploy_started_at=started_at,
            site_url=site_url,
            project_name=project_name,
            missing_secrets=missing,
            secrets_url=f"{repo.html_url}/settings/secrets/actions",
            actions_url=f"{repo.html_url}/actions/workflows/{WORKFLOW_FILE}",
        )

    async def deploy(self, user: UserRead, project_id: UUID, request: DeployRequest) -> DeployStarted:
        """Runs a published site's deploy workflow again, e.g. once the user added the secrets on GitHub. With
        `request.credentials`, saves the token as the platform's secrets first.

        Raises:
            PublishNotPublishedError: The site hasn't been published (no workflow or publish settings).
            PublishInvalidError: Credentials were sent for GitHub Pages.
            PublishSecretsMissingError: The platform's secrets aren't all on the repo yet.
            PlatformTokenInvalidError: The platform refused the pasted token.
            PlatformError: The platform failed.
            PublishDeployNotStartedError: GitHub wouldn't start the deploy.
            The site's repo errors of `publish`.
        """
        token, repo, settings = await self._published(user, project_id)
        prepared = await self._prepare(settings.target, request.credentials, settings.project_name, repo.default_branch)
        if prepared is not None:
            await self._set_secrets(token, repo.full_name, prepared.secrets)
        else:
            missing = await self._missing_secrets(token, repo.full_name, settings.target)
            if missing:
                raise PublishSecretsMissingError(
                    details={"missing": missing, "secretsUrl": f"{repo.html_url}/settings/secrets/actions"}
                )
        started_at = await self._start_deploy(token, repo, repo.default_branch)
        return DeployStarted(
            repo_url=repo.html_url,
            actions_url=f"{repo.html_url}/actions/workflows/{WORKFLOW_FILE}",
            started_at=started_at,
        )

    async def status(self, user: UserRead, project_id: UUID, since: str | None = None) -> PublishStatus:
        """Where a published site is deployed, and how its latest deploy run went. With `since` (a deploy's
        `startedAt`), only a run started then or later, so an earlier run of the same commit isn't taken for it.

        Raises:
            PublishNotPublishedError: The site hasn't been published.
            The site's repo errors of `publish`.
        """
        token, repo, settings = await self._published(user, project_id)
        run = await self._github.latest_workflow_run(
            token, repo.full_name, WORKFLOW_FILE, repo.default_branch, created_since=since
        )
        return PublishStatus(
            target=settings.target,
            site_url=settings.site_url,
            run=WorkflowRun.model_validate(run.model_dump()) if run else None,
        )

    async def _measurement_id(self, project_id: UUID) -> str | None:
        """The Google Analytics tag the site's pages get, if it's linked to a stream. Ends the transaction: the
        publish waits on GitHub for a while."""
        link = await self._analytics.get(project_id)
        measurement_id = link.measurement_id if link else None
        await self._uow.rollback()
        return measurement_id

    async def _published(self, user: UserRead, project_id: UUID) -> tuple[str, GithubRepo, PublishSettings]:
        token, repo = await self._sites.github_repo(user, project_id)
        head = await self._sites.branch_head(token, repo)
        snapshot = await self._sites.snapshot(token, repo, head.commit_sha)
        settings = _settings(_manifest(snapshot))
        if settings is None or WORKFLOW_PATH not in snapshot.files:
            raise PublishNotPublishedError()
        return token, repo, settings

    async def _start_deploy(self, token: str, repo: GithubRepo, branch: str) -> str:
        """Starts the deploy workflow and returns when (UTC, to the second, less a clock-skew margin).

        Raises:
            PublishDeployNotStartedError: GitHub still wouldn't start it after the retries.
        """
        # A little early, in case GitHub's clock is behind ours: its run would look older than the deploy otherwise.
        started = self._clock() - timedelta(seconds=DEPLOY_CLOCK_SKEW_SECONDS)
        started_at = started.replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")
        # A workflow file that was just written can take GitHub a few seconds to register.
        for delay in (*self._dispatch_delays, None):
            try:
                await self._github.dispatch_workflow(token, repo.full_name, WORKFLOW_FILE, branch)
                return started_at
            except GithubNotFoundError:
                if delay is None:
                    break
                await self._sleep(delay)
        details = {"repoName": repo.name, "actionsUrl": f"{repo.html_url}/actions/workflows/{WORKFLOW_FILE}"}
        raise PublishDeployNotStartedError(details=details)

    async def _set_private(self, token: str, project_id: UUID, repo: GithubRepo, *, private: bool) -> GithubRepo:
        repo = await self._github.set_private(token, repo.full_name, private)
        self._sites.forget_repo(project_id, repo.full_name)
        logger.info("site repo visibility changed", extra={"repo": repo.full_name, "private": private})
        return repo

    async def _make_private_again(self, token: str, project_id: UUID, repo: GithubRepo) -> None:
        """Makes a repo made public for a publish that then failed private again, without hiding why it failed."""
        try:
            await self._set_private(token, project_id, repo, private=True)
        except Exception:
            logger.exception(
                "Couldn't make a site repo private again after a failed publish", extra={"repo": repo.full_name}
            )

    async def _prepare(
        self, target: PublishTarget, credentials: PublishCredentials | None, project_name: str | None, branch: str
    ) -> PreparedProject | None:
        if credentials is None:
            return None
        if project_name is None:
            raise PublishInvalidError("GitHub Pages takes no token.")
        return await self._platforms.prepare(
            target,
            credentials.token.get_secret_value(),
            project_name=project_name,
            branch=branch,
            account_id=credentials.account_id,
        )

    async def _set_secrets(self, token: str, full_name: str, secrets: dict[str, str]) -> None:
        key = await self._github.get_actions_key(token, full_name)
        for name, value in secrets.items():
            await self._github.put_actions_secret(token, full_name, name, seal_for_github(key.key, value), key.key_id)

    async def _missing_secrets(self, token: str, full_name: str, target: PublishTarget) -> list[str]:
        names = TARGET_SECRETS[target]
        found = await asyncio.gather(*(self._github.has_actions_secret(token, full_name, n) for n in names))
        return [name for name, present in zip(names, found, strict=True) if not present]


def _site_kind(snapshot: SiteSnapshot) -> SiteKind:
    """React (Vite) when the site has src/main.tsx, static when it has index.html only.

    Raises:
        PublishInvalidError: Neither.
    """
    if "src/main.tsx" in snapshot.files:
        return "react"
    if "index.html" in snapshot.files:
        return "static"
    raise PublishInvalidError("The site needs src/main.tsx or index.html to be published.")


def _manifest(snapshot: SiteSnapshot) -> dict[str, object]:
    """The site's manifest (the site's repo was checked to have one, naming this site)."""
    try:
        manifest = json.loads(snapshot.files.get(MANIFEST_PATH, "{}"))
    except ValueError:
        manifest = {}
    return manifest if isinstance(manifest, dict) else {}


def _settings(manifest: dict[str, object]) -> PublishSettings | None:
    """Where the site was last published, if it was."""
    try:
        return PublishSettings.model_validate(manifest["publish"])
    except KeyError, ValidationError:
        return None


def _check_branch(branch: str) -> str:
    if not _BRANCH.fullmatch(branch):
        raise PublishInvalidError("Rename the repository's default branch to `main` to publish it.")
    return branch


def _default_project(login: str, repo_name: str) -> str:
    """`<login>-<repo>` as a name every platform accepts. Platform names are global, so the login keeps a common
    repo name from being taken already."""
    slug = re.sub(r"[^a-z0-9]+", "-", f"{login}-{repo_name}".lower()).strip("-")
    return slug[:PROJECT_NAME_MAX].rstrip("-") or "makable-site"


def _is_user_site(login: str, repo_name: str) -> bool:
    return repo_name.lower() == f"{login.lower()}.github.io"


def _pages_base_path(login: str, repo_name: str) -> str:
    return "/" if _is_user_site(login, repo_name) else f"/{repo_name}/"


def _site_url(
    login: str,
    repo_name: str,
    target: PublishTarget,
    project_name: str | None,
    prepared: PreparedProject | None,
    published: PublishSettings | None,
) -> str | None:
    """Where the site will be: the platform's answer when it was asked, else what the last publish learned, else the
    platform's usual address for the project."""
    if target == PublishTarget.GITHUB_PAGES:
        return f"https://{login.lower()}.github.io{_pages_base_path(login, repo_name)}"
    if prepared is not None:
        return prepared.site_url
    if published is not None and published.target == target and published.project_name == project_name:
        return published.site_url
    domains = {
        PublishTarget.VERCEL: "vercel.app",
        PublishTarget.CLOUDFLARE_PAGES: "pages.dev",
        PublishTarget.NETLIFY: "netlify.app",
    }
    return f"https://{project_name}.{domains[target]}"
