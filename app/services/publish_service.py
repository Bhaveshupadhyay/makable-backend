import asyncio
import json
import logging
import re
from collections.abc import Awaitable, Callable, Sequence
from uuid import UUID

from fastapi import status
from pydantic import ValidationError

from app.clients.deploy_platforms import DeployPlatformClient, PreparedProject
from app.clients.github import GithubClient, GithubConflictError, GithubNotFoundError, GithubRepoExistsError
from app.clients.templates import TemplateCatalogClient
from app.constants.publish import (
    DEFAULT_REPO,
    MANIFEST_FORMAT,
    MANIFEST_PATH,
    MANIFEST_VERSION,
    MAX_CONFLICT_PATHS,
    MAX_FILE_EDITS,
    NEW_REPO_RETRY_DELAYS,
    PROJECT_NAME_MAX,
    RESERVED_PATH,
    SITE_DESCRIPTION,
    TARGET_NAMES,
    TARGET_SECRETS,
    TARGET_TOKEN_URLS,
    WORKFLOW_FILE,
    WORKFLOW_PATH,
    PublishTarget,
)
from app.constants.workspace import WORKSPACE_REPO
from app.core.exceptions import AppError, ConflictError, NotFoundError
from app.core.security import TokenCipher, seal_for_github
from app.repositories.github_credential import GithubCredentialRepository
from app.schemas.github import GithubBranchHead, GithubRepo, GithubTreeChange
from app.schemas.portfolio import Portfolio
from app.schemas.publish import (
    DeployRequest,
    DeployStarted,
    DeployState,
    PublishCredentials,
    PublishRequest,
    PublishResult,
    PublishStatus,
    PublishTargetInfo,
    SiteManifest,
    WorkflowRun,
)
from app.schemas.template import TemplateEntry, TemplateFiles
from app.schemas.user import UserRead
from app.services.publish_workflows import render_workflow
from app.services.workspace_service import WorkspaceService, git_blob_sha
from app.utils.portfolio_source import render_portfolio_source

logger = logging.getLogger(__name__)

# Branch names the workflow can use as they are.
_BRANCH = re.compile(r"[A-Za-z0-9._/-]{1,100}")


class PublishGithubNotConnectedError(AppError):
    status_code = status.HTTP_401_UNAUTHORIZED
    code = "github_reconnect"
    message = "Connect GitHub again to publish your site."


class PublishMissingScopeError(AppError):
    """The user signed in before makable asked for the `workflow` scope, which writing a workflow file needs."""

    status_code = status.HTTP_403_FORBIDDEN
    code = "github_missing_scope"
    message = "Connect GitHub again to let makable set up your site's deploy workflow."


class PublishInvalidError(AppError):
    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    code = "publish_invalid"
    message = "This site can't be published like that."


class PublishRepoTakenError(ConflictError):
    code = "publish_repo_taken"
    message = "You already have a repository with that name that makable didn't create. Pick another name."


class PublishRepoArchivedError(ConflictError):
    code = "publish_repo_archived"
    message = "That repository is archived on GitHub. Unarchive it, or publish to a new repository."


class PublishPagesPrivateError(ConflictError):
    code = "publish_pages_private"
    message = "GitHub Pages needs a public repository. Make it public on GitHub, or pick another platform."


class PublishChangedOnGithubError(ConflictError):
    """Files makable manages were changed on GitHub since the last publish. `details` has the `headSha` to send
    back as `confirmOverwrite` and the changed `paths`."""

    code = "publish_changed_on_github"
    message = "This site was changed on GitHub since it was last published. Publishing will overwrite those changes."


class PublishRepoNotFoundError(NotFoundError):
    code = "publish_repo_not_found"
    message = "This site hasn't been published to that repository."


class PublishSecretsMissingError(ConflictError):
    """`details` has the `missing` secret names and the `secretsUrl` where the user adds them."""

    code = "publish_secrets_missing"
    message = "Add the deploy secrets to the repository on GitHub first."


class PublishService:
    """Publishes a site to a repo on the user's GitHub, with a workflow that deploys it to the platform they picked
    (GitHub Pages, Vercel, Cloudflare Pages or Netlify) on every push.

    The site is published as saved in the user's private `makable-workspace` repo (the browser saves, then sends
    the saved state's SHA): its content and AI-edited files are read from there, the template comes from the
    catalog, and the deploy workflow is written here. A publish is one commit.

    A platform token pasted in the chat is checked, used to create the platform project, and saved as an Actions
    secret (sealed so only GitHub can read it); makable stores nothing. Otherwise the user adds the secrets on
    GitHub, and the workflow skips the deploy until they're there.

    makable only writes to a repo whose `.makable/manifest.json` names this site, and never overwrites a managed
    file that was changed on GitHub without the user's say-so.
    """

    def __init__(
        self,
        github: GithubClient,
        credentials: GithubCredentialRepository,
        cipher: TokenCipher,
        templates: TemplateCatalogClient,
        platforms: DeployPlatformClient,
        workspace: WorkspaceService,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        retry_delays: Sequence[float] = NEW_REPO_RETRY_DELAYS,
    ) -> None:
        self._github = github
        self._credentials = credentials
        self._cipher = cipher
        self._templates = templates
        self._platforms = platforms
        self._workspace = workspace
        self._sleep = sleep
        self._retry_delays = retry_delays

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
        """Commits the site as saved with `request.state_sha` and its deploy workflow to the site repo (created on
        first publish), which starts the deploy. With `request.credentials`, first checks the token, creates the
        platform project and saves the token as an Actions secret.

        Raises:
            PublishInvalidError: No template picked yet, an edited file isn't a template file, or the repo or
                branch name can't be used.
            WorkspaceConflictError: The saved session isn't `state_sha` any more (another device saved since).
            WorkspaceSessionNotFoundError: The site isn't saved to the workspace.
            WorkspaceInvalidError: The saved session can't be read, or has too many AI-edited files.
            WorkspacePartNotFoundError: A file the saved state lists is missing.
            PublishRepoTakenError: The repo exists, but makable didn't create it for this site.
            PublishRepoArchivedError: The repo is archived.
            PublishPagesPrivateError: GitHub Pages was picked for a private repo.
            PublishChangedOnGithubError: Managed files changed on GitHub and the user hasn't agreed to overwrite.
            PublishGithubNotConnectedError: No usable GitHub token.
            PublishMissingScopeError: The GitHub token can't write workflow files.
            TemplateNotFoundError: The template isn't in the catalog.
            TemplateUnavailableError: The catalog couldn't be read.
            PlatformTokenInvalidError: The platform refused the pasted token.
            PlatformAccountRequiredError: The Cloudflare token sees several accounts and none was picked.
            PlatformProjectTakenError: The platform project name belongs to someone else.
            PlatformError: The platform failed.
            GithubPagesUnavailableError: GitHub won't turn Pages on for the repo.
            GithubError: GitHub failed.
        """
        target = request.target
        pages = target == PublishTarget.GITHUB_PAGES
        repo_name = request.repo_name or _default_repo(user.login, target)
        if repo_name.lower() == WORKSPACE_REPO:
            raise PublishInvalidError(f"{WORKSPACE_REPO} keeps your chat history; publish to another repository.")
        token = await self._token(user)
        scopes = await self._github.get_scopes(token)
        if scopes is not None and "workflow" not in scopes:
            raise PublishMissingScopeError()
        snapshot = await self._workspace.read_snapshot(user, project_id, request.state_sha, max_files=MAX_FILE_EDITS)
        portfolio = snapshot.state.conversation.portfolio
        if portfolio is None:
            raise PublishInvalidError("Pick a template before publishing.")
        entry = await self._templates.get_template(portfolio.template)
        files = build_site_files(entry, await self._templates.get_files(entry), portfolio, snapshot.files)

        full_name = f"{user.login}/{repo_name}"
        repo = await self._github.get_repo(token, full_name)
        manifest = await self._check_repo(token, repo, project_id, pages) if repo else None
        if repo is None:
            repo = await self._create_repo(token, repo_name, private=bool(request.private))
        branch = _check_branch(repo.default_branch)

        project_name = None
        if not pages:
            same = manifest is not None and manifest.target == target
            project_name = (
                request.project_name
                or (manifest.project_name if same and manifest else None)
                or _default_project(user.login, repo.name)
            )
        prepared = await self._prepare(target, request.credentials, project_name, branch)
        site_url = _site_url(user.login, repo.name, target, project_name, prepared, manifest)

        files[WORKFLOW_PATH] = render_workflow(
            target,
            entry.kind,
            branch=branch,
            base_path=_pages_base_path(user.login, repo.name) if pages else "/",
            project_name=project_name,
        )
        hashes = {path: git_blob_sha(content) for path, content in files.items()}
        new_manifest = SiteManifest(
            format=MANIFEST_FORMAT,
            version=MANIFEST_VERSION,
            project_id=project_id,
            target=target,
            template_id=entry.id,
            template_version=entry.version,
            project_name=project_name,
            site_url=site_url,
            files=hashes,
        )
        manifest_content = json.dumps(new_manifest.model_dump(mode="json", by_alias=True), indent=2) + "\n"

        # Before the commit, so the run its push starts can deploy straight away.
        if pages:
            await self._github.enable_pages_workflow(token, repo.full_name)
        if prepared is not None:
            await self._set_secrets(token, repo.full_name, prepared.secrets)

        # Two tries: the branch can move between reading it and committing.
        for attempt in range(2):
            head = await self._head(token, repo)
            tree = await self._github.get_tree(token, repo.full_name, head.tree_sha)
            current = await self._read_manifest(token, repo) if attempt else manifest
            if current is not None and request.confirm_overwrite != head.commit_sha:
                changed = [path for path, sha in current.files.items() if tree.get(path) != sha]
                if changed:
                    details = {"headSha": head.commit_sha, "paths": changed[:MAX_CONFLICT_PATHS]}
                    raise PublishChangedOnGithubError(details=details)
            changes = [
                GithubTreeChange(path=path, content=content)
                for path, content in files.items()
                if tree.get(path) != hashes[path]
            ]
            if current is not None:
                changes += [
                    GithubTreeChange(path=path, content=None)
                    for path in current.files
                    if path not in files and path in tree
                ]
            if tree.get(MANIFEST_PATH) != git_blob_sha(manifest_content):
                changes.append(GithubTreeChange(path=MANIFEST_PATH, content=manifest_content))
            if not changes:
                commit_sha, committed = head.commit_sha, False
                break
            try:
                commit_sha = await self._github.commit_changes(
                    token, repo.full_name, branch, head, changes, message=_commit_message(target, changes)
                )
                committed = True
                break
            except GithubConflictError:
                if attempt == 1:
                    raise PublishChangedOnGithubError() from None

        missing = [] if prepared else await self._missing_secrets(token, repo.full_name, target)
        deploy: DeployState
        if missing:
            deploy = "waiting_for_secrets"
        elif committed:
            deploy = "started"
        elif prepared is not None:
            # Nothing to commit, but there's a new token: deploy with it.
            await self._github.dispatch_workflow(token, repo.full_name, WORKFLOW_FILE, branch)
            deploy = "started"
        else:
            deploy = "unchanged"
        logger.info(
            "site published",
            extra={"user_id": str(user.id), "project_id": str(project_id), "target": target, "deploy": deploy},
        )
        return PublishResult(
            target=target,
            repo_full_name=repo.full_name,
            repo_url=repo.html_url,
            branch=branch,
            commit_sha=commit_sha,
            deploy=deploy,
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
            PublishRepoNotFoundError: The repo doesn't exist, or holds no makable site.
            PublishRepoTakenError: The repo holds another site.
            PublishInvalidError: Credentials were sent for GitHub Pages.
            PublishSecretsMissingError: The platform's secrets aren't all on the repo yet.
            PublishGithubNotConnectedError: No usable GitHub token.
            PlatformTokenInvalidError: The platform refused the pasted token.
            PlatformError: The platform failed.
            GithubError: GitHub failed.
        """
        token = await self._token(user)
        repo, manifest = await self._published(token, user, project_id, request.repo_name)
        prepared = await self._prepare(manifest.target, request.credentials, manifest.project_name, repo.default_branch)
        if prepared is not None:
            await self._set_secrets(token, repo.full_name, prepared.secrets)
        else:
            missing = await self._missing_secrets(token, repo.full_name, manifest.target)
            if missing:
                raise PublishSecretsMissingError(
                    details={"missing": missing, "secretsUrl": f"{repo.html_url}/settings/secrets/actions"}
                )
        try:
            await self._github.dispatch_workflow(token, repo.full_name, WORKFLOW_FILE, repo.default_branch)
        except GithubNotFoundError:
            raise PublishRepoNotFoundError("The deploy workflow is missing. Publish the site again.") from None
        return DeployStarted(repo_url=repo.html_url, actions_url=f"{repo.html_url}/actions/workflows/{WORKFLOW_FILE}")

    async def status(self, user: UserRead, project_id: UUID, repo_name: str) -> PublishStatus:
        """Where a published site is deployed, and how its latest deploy run went.

        Raises:
            PublishRepoNotFoundError: The repo doesn't exist, or holds no makable site.
            PublishRepoTakenError: The repo holds another site.
            PublishGithubNotConnectedError: No usable GitHub token.
            GithubError: GitHub failed.
        """
        token = await self._token(user)
        repo, manifest = await self._published(token, user, project_id, repo_name)
        run = await self._github.latest_workflow_run(token, repo.full_name, WORKFLOW_FILE, repo.default_branch)
        return PublishStatus(
            target=manifest.target,
            site_url=manifest.site_url,
            run=WorkflowRun.model_validate(run.model_dump()) if run else None,
        )

    async def _token(self, user: UserRead) -> str:
        credential = await self._credentials.get(user.id)
        if credential is None:
            raise PublishGithubNotConnectedError()
        return self._cipher.decrypt(credential.access_token)

    async def _check_repo(self, token: str, repo: GithubRepo, project_id: UUID, pages: bool) -> SiteManifest | None:
        """The manifest of an existing repo makable may publish this site to (None: a setup that didn't finish).

        Raises:
            PublishRepoTakenError: makable didn't create the repo, or it holds another site.
            PublishRepoArchivedError: The repo is archived.
            PublishPagesPrivateError: GitHub Pages on a private repo.
        """
        manifest = await self._read_manifest(token, repo)
        if manifest is None and not await self._unfinished_setup(token, repo):
            raise PublishRepoTakenError()
        if manifest is not None and manifest.project_id != project_id:
            raise PublishRepoTakenError("That repository has another makable site. Pick another name.")
        if repo.archived:
            raise PublishRepoArchivedError()
        if pages and repo.private:
            raise PublishPagesPrivateError()
        return manifest

    async def _published(
        self, token: str, user: UserRead, project_id: UUID, repo_name: str
    ) -> tuple[GithubRepo, SiteManifest]:
        repo = await self._github.get_repo(token, f"{user.login}/{repo_name}")
        manifest = await self._read_manifest(token, repo) if repo else None
        if repo is None or manifest is None:
            raise PublishRepoNotFoundError()
        if manifest.project_id != project_id:
            raise PublishRepoTakenError("That repository has another makable site.")
        return repo, manifest

    async def _read_manifest(self, token: str, repo: GithubRepo) -> SiteManifest | None:
        file = await self._github.get_file(token, repo.full_name, MANIFEST_PATH)
        if file is None:
            return None
        try:
            return SiteManifest.model_validate(json.loads(file.content))
        except ValueError, ValidationError:
            # Edited by hand into something else: not a repo makable can safely write to.
            raise PublishRepoTakenError("That repository's makable manifest is damaged.") from None

    async def _unfinished_setup(self, token: str, repo: GithubRepo) -> bool:
        """Whether a repo without a manifest is one makable created whose first publish didn't finish (e.g. the
        platform refused the token right after): makable's description and nothing but GitHub's README."""
        if repo.description != SITE_DESCRIPTION:
            return False
        entries = await self._github.list_dir(token, repo.full_name, "")
        return [e.name for e in entries] == ["README.md"]

    async def _create_repo(self, token: str, name: str, *, private: bool) -> GithubRepo:
        try:
            repo = await self._github.create_repo(token, name, SITE_DESCRIPTION, private=private)
        except GithubRepoExistsError:
            # Created a moment ago by another tab, or GitHub only now shows a repo we couldn't see.
            raise PublishRepoTakenError("A repository with that name was just created. Try publishing again.") from None
        logger.info("site repo created", extra={"repo": repo.full_name})
        return repo

    async def _head(self, token: str, repo: GithubRepo) -> GithubBranchHead:
        # A new repo can take a moment to show its first commit.
        for delay in (*self._retry_delays, None):
            try:
                return await self._github.get_branch_head(token, repo.full_name, repo.default_branch)
            except GithubNotFoundError:
                if delay is None:
                    raise
                await self._sleep(delay)
        raise AssertionError("unreachable")

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


def build_site_files(
    entry: TemplateEntry, template: TemplateFiles, portfolio: Portfolio, file_edits: dict[str, str]
) -> dict[str, str | bytes]:
    """The site's files, like the preview builds them: the template's files, the AI-edited ones over them, and the
    content file rendered from `portfolio`. Binary files (images) are published too, though the preview skips them.

    Raises:
        PublishInvalidError: An edited file isn't one of the template's text files, or is the content file.
    """
    for path in file_edits:
        if path not in template.text or path == entry.content_path or RESERVED_PATH.match(path):
            raise PublishInvalidError(f"{path} isn't a file of the template that can be edited.")
    # Themed React templates read their look from the content's `template` field.
    content = portfolio.model_copy(update={"template": entry.theme}) if entry.theme else portfolio
    files: dict[str, str | bytes] = {**template.text, **file_edits, **template.binary}
    files[entry.content_path] = render_portfolio_source(content, entry.kind)
    return files


def _check_branch(branch: str) -> str:
    if not _BRANCH.fullmatch(branch):
        raise PublishInvalidError("Rename the repository's default branch to `main` to publish it.")
    return branch


def _default_repo(login: str, target: PublishTarget) -> str:
    return f"{login.lower()}.github.io" if target == PublishTarget.GITHUB_PAGES else DEFAULT_REPO


def _default_project(login: str, repo_name: str) -> str:
    """`<login>-<repo>` as a name every platform accepts. Platform names are global, so the login keeps a common
    repo name like `portfolio` from being taken already."""
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
    manifest: SiteManifest | None,
) -> str | None:
    """Where the site will be: the platform's answer when it was asked, else what the last publish learned, else
    the platform's usual address for the project."""
    if target == PublishTarget.GITHUB_PAGES:
        return f"https://{login.lower()}.github.io{_pages_base_path(login, repo_name)}"
    if prepared is not None:
        return prepared.site_url
    if manifest is not None and manifest.target == target and manifest.project_name == project_name:
        return manifest.site_url
    domains = {
        PublishTarget.VERCEL: "vercel.app",
        PublishTarget.CLOUDFLARE_PAGES: "pages.dev",
        PublishTarget.NETLIFY: "netlify.app",
    }
    return f"https://{project_name}.{domains[target]}"


def _commit_message(target: PublishTarget, changes: list[GithubTreeChange]) -> str:
    changed = sum(1 for c in changes if c.content is not None)
    removed = len(changes) - changed
    detail = f"{changed} changed" + (f", {removed} removed" if removed else "")
    return f"Publish from makable to {TARGET_NAMES[target]} ({detail})"
