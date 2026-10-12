import json
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
import yaml

from app.clients.deploy_platforms import PlatformTokenInvalidError
from app.constants.publish import MANIFEST_PATH, SITE_DESCRIPTION, WORKFLOW_PATH, PublishTarget
from app.schemas.github import GithubWorkflowRun
from app.schemas.publish import DeployRequest, PublishRequest
from app.schemas.workspace import WorkspaceSave
from app.services.publish_service import (
    PublishChangedOnGithubError,
    PublishDeployNotStartedError,
    PublishGithubNotConnectedError,
    PublishInvalidError,
    PublishMissingScopeError,
    PublishPagesPrivateError,
    PublishRepoArchivedError,
    PublishRepoNotFoundError,
    PublishRepoTakenError,
    PublishSecretsMissingError,
    PublishService,
)
from app.services.workspace_service import (
    VerifiedRepos,
    WorkspaceConflictError,
    WorkspacePartNotFoundError,
    WorkspaceService,
    WorkspaceSessionNotFoundError,
    git_blob_sha,
)
from app.tests.ai_edit_fixtures import PORTFOLIO, render_portfolio_source
from app.tests.fakes import VALID_PLATFORM_TOKEN, FakeDeployPlatforms, FakeGithubClient, FakeTemplateCatalog
from app.tests.services.test_workspace_service import CIPHER, USER, FakeCredentials, no_sleep
from app.tests.session_fixtures import PROJECT_ID, save_body

PAGES_REPO = "octocat/octocat.github.io"
NOW = datetime(2026, 10, 11, 13, 45, 7, 512000, tzinfo=UTC)
SITE_REPO = "octocat/portfolio"


class Setup:
    def __init__(self, stored: str | None = "gho_token") -> None:
        self.github = FakeGithubClient()
        self.templates = FakeTemplateCatalog()
        self.platforms = FakeDeployPlatforms()
        credentials = FakeCredentials(stored)
        self.workspace = WorkspaceService(self.github, credentials, CIPHER, sleep=no_sleep, verified=VerifiedRepos())
        self.service = PublishService(
            self.github,
            credentials,
            CIPHER,
            self.templates,
            self.platforms,
            self.workspace,
            sleep=no_sleep,
            retry_delays=(0.1,),
            dispatch_delays=(0.1, 0.1),
            clock=lambda: NOW,
        )
        self.sha: str | None = None
        self._saved: Any = None

    async def save(self, portfolio: dict[str, Any] | None, files: dict[str, str]) -> str:
        """Saves the session like the SPA does before publishing: the state and its AI-edited files."""
        template = portfolio["template"] if portfolio else "minimal"
        parts = [{"path": f"files/{template}/{path}", "content": content} for path, content in files.items()]
        body = save_body(
            self.sha,
            parts,
            conversation={"step": "done", "portfolio": portfolio},
            files={template: list(files)} if files else {},
            aiHistory=[],
        )
        saved = await self.workspace.save(USER, PROJECT_ID, WorkspaceSave.model_validate(body))
        assert saved.sha is not None
        self.sha = saved.sha
        return saved.sha

    async def publish(
        self,
        *,
        portfolio: dict[str, Any] = PORTFOLIO,
        fileEdits: dict[str, str] | None = None,  # the SPA's name
        templateId: str | None = None,
        **over: Any,
    ) -> Any:
        """Saves the session if it changed, then publishes it (what the SPA's Publish does)."""
        content = {**portfolio, "template": templateId} if templateId else portfolio
        session = (content, fileEdits or {})
        if session != self._saved:
            await self.save(*session)
            self._saved = session
        body = {"target": "github-pages", "stateSha": self.sha, **over}
        return await self.service.publish(USER, PROJECT_ID, PublishRequest.model_validate(body))

    def files(self, repo: str) -> dict[str, str]:
        return {path: file.content for path, file in self.github.files[repo].items()}

    def commits(self, repo: str) -> list[tuple[str, list[str]]]:
        """The commits to a site repo (the workspace saves are commits too)."""
        return [(message, paths) for name, message, paths in self.github.commits if name == repo]

    def workflow(self, repo: str) -> Any:
        return yaml.safe_load(self.files(repo)[WORKFLOW_PATH])


async def test_the_first_github_pages_publish_creates_the_user_site_and_commits_it_once() -> None:
    s = Setup()

    result = await s.publish(fileEdits={"src/index.css": "body { color: red }\n"})

    repo = s.github.repos[PAGES_REPO]
    assert not repo.private and repo.description == SITE_DESCRIPTION
    files = s.files(PAGES_REPO)
    # The template, the AI-edited file over it, and the content file rendered like the preview (themed).
    assert files["src/App.tsx"] == s.templates.react_files.text["src/App.tsx"]
    assert files["src/index.css"] == "body { color: red }\n"
    assert files["src/content/portfolio.ts"] == render_portfolio_source(PORTFOLIO)
    assert files["README.md"] == "# octocat.github.io\n"  # GitHub's own, left alone
    manifest = json.loads(files[MANIFEST_PATH])
    assert manifest["projectId"] == str(PROJECT_ID) and manifest["target"] == "github-pages"
    assert manifest["files"]["src/index.css"] == git_blob_sha("body { color: red }\n")
    assert WORKFLOW_PATH in manifest["files"] and MANIFEST_PATH not in manifest["files"]
    [(message, _)] = s.commits(PAGES_REPO)
    assert message.startswith("Publish from makable to GitHub Pages")
    assert PAGES_REPO in s.github.pages
    assert s.workflow(PAGES_REPO)["env"]["BASE_PATH"] == "/"
    assert result.deploy == "started" and result.missing_secrets == [] and result.changed
    # makable starts the deploy itself (the workflow has no push trigger), and says when, to the second.
    assert s.github.dispatches == [(PAGES_REPO, "makable-deploy.yml", "main")]
    assert result.deploy_started_at == "2026-10-11T13:44:57Z"
    assert result.site_url == "https://octocat.github.io/"
    assert result.repo_url == f"https://github.com/{PAGES_REPO}"
    assert result.actions_url == f"https://github.com/{PAGES_REPO}/actions/workflows/makable-deploy.yml"


async def test_a_pages_project_site_is_served_under_the_repo_name() -> None:
    s = Setup()

    result = await s.publish(repoName="my-site")

    assert result.site_url == "https://octocat.github.io/my-site/"
    assert s.workflow("octocat/my-site")["env"]["BASE_PATH"] == "/my-site/"


async def test_a_token_pasted_in_the_chat_sets_up_the_project_and_is_saved_as_a_sealed_secret() -> None:
    s = Setup()

    result = await s.publish(target="vercel", credentials={"token": VALID_PLATFORM_TOKEN})

    assert s.platforms.prepared == [(PublishTarget.VERCEL, "octocat-portfolio", "main", None)]
    # The fake GitHub opens the sealed box with the repo's private key: GitHub gets exactly the token.
    assert s.github.secrets[SITE_REPO] == {"VERCEL_TOKEN": VALID_PLATFORM_TOKEN}
    assert result.deploy == "started" and result.missing_secrets == []
    assert result.site_url == "https://octocat-portfolio-1a2b.example.app"
    assert result.project_name == "octocat-portfolio"
    assert s.workflow(SITE_REPO)["env"]["PROJECT_NAME"] == "octocat-portfolio"
    assert VALID_PLATFORM_TOKEN not in json.dumps(s.files(SITE_REPO))
    assert SITE_REPO not in s.github.pages


async def test_without_a_token_the_user_adds_the_secrets_on_github_then_deploys() -> None:
    s = Setup()

    result = await s.publish(target="cloudflare-pages")

    assert result.deploy == "waiting_for_secrets" and result.deploy_started_at is None
    assert s.github.dispatches == []  # not started without the secrets
    assert result.missing_secrets == ["CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID"]
    assert result.secrets_url == f"https://github.com/{SITE_REPO}/settings/secrets/actions"
    assert result.site_url == "https://octocat-portfolio.pages.dev"
    assert s.platforms.prepared == []
    deploy = DeployRequest(repo_name="portfolio")
    with pytest.raises(PublishSecretsMissingError) as missing:
        await s.service.deploy(USER, PROJECT_ID, deploy)
    assert missing.value.details["missing"] == ["CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID"]

    s.github.secrets[SITE_REPO] = {"CLOUDFLARE_API_TOKEN": "t", "CLOUDFLARE_ACCOUNT_ID": "a"}
    started = await s.service.deploy(USER, PROJECT_ID, deploy)

    assert s.github.dispatches == [(SITE_REPO, "makable-deploy.yml", "main")]
    assert started.actions_url.endswith("/actions/workflows/makable-deploy.yml")
    assert started.started_at == "2026-10-11T13:44:57Z"
    again = await s.publish(target="cloudflare-pages")
    assert (again.deploy, again.changed) == ("started", False)
    assert len(s.github.dispatches) == 2


async def test_a_token_can_be_pasted_after_publishing() -> None:
    s = Setup()
    await s.publish(target="netlify")

    await s.service.deploy(
        USER,
        PROJECT_ID,
        DeployRequest.model_validate({"repoName": "portfolio", "credentials": {"token": VALID_PLATFORM_TOKEN}}),
    )

    assert s.github.secrets[SITE_REPO] == {"NETLIFY_AUTH_TOKEN": VALID_PLATFORM_TOKEN}
    assert s.github.dispatches == [(SITE_REPO, "makable-deploy.yml", "main")]


async def test_a_republish_only_commits_what_changed() -> None:
    s = Setup()
    await s.publish()

    unchanged = await s.publish()
    assert not unchanged.changed and len(s.commits(PAGES_REPO)) == 1

    await s.publish(portfolio={**PORTFOLIO, "skills": ["Rust"]})
    assert s.commits(PAGES_REPO)[-1][1] == ["src/content/portfolio.ts", MANIFEST_PATH]


async def test_every_publish_deploys_even_with_nothing_new_to_commit() -> None:
    s = Setup()
    await s.publish(target="vercel", credentials={"token": VALID_PLATFORM_TOKEN})
    # A file the user added on GitHub isn't live until they publish (pushes don't deploy).
    s.github.edit_on_github(SITE_REPO, "CNAME", "me.dev\n")
    assert len(s.github.dispatches) == 1

    result = await s.publish(target="vercel")

    assert (result.deploy, result.changed) == ("started", False)
    assert s.github.dispatches == [(SITE_REPO, "makable-deploy.yml", "main")] * 2


async def test_starting_the_deploy_waits_for_github_to_register_the_workflow() -> None:
    s = Setup()
    s.github.dispatch_not_ready = 2

    result = await s.publish()

    assert result.deploy == "started" and len(s.github.dispatches) == 1


async def test_a_deploy_github_wont_start_says_the_code_is_there() -> None:
    s = Setup()
    s.github.dispatch_not_ready = 99

    with pytest.raises(PublishDeployNotStartedError) as err:
        await s.publish(target="netlify", credentials={"token": VALID_PLATFORM_TOKEN})

    assert err.value.details == {
        "repoName": "portfolio",
        "actionsUrl": f"https://github.com/{SITE_REPO}/actions/workflows/makable-deploy.yml",
    }
    assert len(s.commits(SITE_REPO)) == 1  # the site is committed; deploying again starts it


async def test_switching_template_and_platform_replaces_makables_files_only() -> None:
    s = Setup()
    await s.publish(target="netlify")
    s.github.edit_on_github(SITE_REPO, "CNAME", "me.dev\n")  # the user's own file

    await s.publish(target="vercel", templateId="paper")

    files = s.files(SITE_REPO)
    assert "src/App.tsx" not in files and "package.json" not in files
    assert files["content/portfolio.js"].startswith("// All copy on the site lives here.")
    assert files["assets/me.png"] == "iVBORw0KGgo="  # the image, committed as bytes
    assert files["CNAME"] == "me.dev\n"
    assert "Deploy to Vercel" in files[WORKFLOW_PATH]
    # The project name stays with the repo when only the platform changes.
    assert json.loads(files[MANIFEST_PATH])["projectName"] == "octocat-portfolio"


async def test_a_managed_file_changed_on_github_needs_the_users_ok() -> None:
    s = Setup()
    await s.publish()
    s.github.edit_on_github(PAGES_REPO, "src/index.css", "body { color: blue }\n")
    s.github.edit_on_github(PAGES_REPO, "README.md", "# Mine\n")  # not managed: no question
    head = s.github.heads[PAGES_REPO].commit_sha

    with pytest.raises(PublishChangedOnGithubError) as conflict:
        await s.publish()

    assert conflict.value.details == {"headSha": head, "paths": ["src/index.css"]}
    await s.publish(confirmOverwrite=head)
    assert s.files(PAGES_REPO)["src/index.css"] == s.templates.react_files.text["src/index.css"]
    assert s.files(PAGES_REPO)["README.md"] == "# Mine\n"


async def test_a_managed_file_deleted_on_github_is_a_change_too() -> None:
    s = Setup()
    await s.publish()
    s.github.edit_on_github(PAGES_REPO, "src/App.tsx", None)

    with pytest.raises(PublishChangedOnGithubError):
        await s.publish()


async def test_a_branch_that_moves_mid_publish_is_retried() -> None:
    s = Setup()
    await s.publish()
    s.github.races = 1

    await s.publish(portfolio={**PORTFOLIO, "skills": ["Go"]})

    assert '"Go"' in s.files(PAGES_REPO)["src/content/portfolio.ts"]


async def test_never_writes_to_a_repo_makable_didnt_create_for_this_site() -> None:
    s = Setup()
    s.github.add_repo("portfolio", private=False, files={"index.html": "mine"})
    with pytest.raises(PublishRepoTakenError):
        await s.publish(target="netlify")

    other = {"format": "makable-site", "version": 1, "projectId": str(uuid4()), "target": "netlify"}
    other |= {"templateId": "minimal", "templateVersion": 1, "files": {}}
    s.github.add_repo("other", private=False, files={MANIFEST_PATH: json.dumps(other)})
    with pytest.raises(PublishRepoTakenError):
        await s.publish(target="netlify", repoName="other")

    s.github.add_repo("broken", private=False, files={MANIFEST_PATH: "{"})
    with pytest.raises(PublishRepoTakenError):
        await s.publish(target="netlify", repoName="broken")
    assert s.commits(SITE_REPO) == []


async def test_a_first_publish_that_stopped_after_creating_the_repo_resumes() -> None:
    s = Setup()
    with pytest.raises(PlatformTokenInvalidError):
        await s.publish(target="vercel", credentials={"token": "wrong"})
    assert s.commits(SITE_REPO) == [] and SITE_REPO in s.github.repos

    result = await s.publish(target="vercel", credentials={"token": VALID_PLATFORM_TOKEN})

    assert result.deploy == "started"


async def test_pages_needs_a_public_repo_and_an_unarchived_one() -> None:
    s = Setup()
    s.github.add_repo("private-site", private=True, description=SITE_DESCRIPTION, files={"README.md": "x"})
    with pytest.raises(PublishPagesPrivateError):
        await s.publish(repoName="private-site")
    archived = s.github.add_repo("old", private=False, description=SITE_DESCRIPTION, files={"README.md": "x"})
    s.github.repos[archived.full_name] = archived.model_copy(update={"archived": True})
    with pytest.raises(PublishRepoArchivedError):
        await s.publish(repoName="old")


async def test_a_private_repo_for_other_platforms() -> None:
    s = Setup()
    await s.publish(target="netlify", private=True)
    assert s.github.repos[SITE_REPO].private


@pytest.mark.parametrize(
    "over",
    [
        {"fileEdits": {"src/new.ts": "x"}},  # not a template file
        {"fileEdits": {"src/content/portfolio.ts": "x"}},  # the content file comes from the portfolio
        {"repoName": "Makable-Workspace"},  # the chat history repo
    ],
)
async def test_refuses_what_it_shouldnt_publish(over: dict[str, Any]) -> None:
    s = Setup()
    with pytest.raises(PublishInvalidError):
        await s.publish(**over)
    assert PAGES_REPO not in s.github.repos


async def test_a_token_without_the_workflow_scope_must_reconnect() -> None:
    s = Setup()
    s.github.scopes = {"repo"}
    with pytest.raises(PublishMissingScopeError):
        await s.publish()

    request = PublishRequest.model_validate({"target": "github-pages", "stateSha": "a" * 40})
    with pytest.raises(PublishGithubNotConnectedError):
        await Setup(stored=None).service.publish(USER, PROJECT_ID, request)


async def test_status_reads_the_latest_run() -> None:
    s = Setup()
    with pytest.raises(PublishRepoNotFoundError):
        await s.service.status(USER, PROJECT_ID, "portfolio")
    await s.publish(target="netlify")
    assert (await s.service.status(USER, PROJECT_ID, "portfolio")).run is None
    s.github.runs[SITE_REPO] = GithubWorkflowRun(
        status="completed",
        conclusion="success",
        head_sha="a" * 40,
        html_url="https://github.com/octocat/portfolio/actions/runs/1",
        created_at="2026-10-11T10:00:00Z",
    )

    status = await s.service.status(USER, PROJECT_ID, "portfolio")
    # A run from before the deploy being followed isn't taken for it.
    assert (await s.service.status(USER, PROJECT_ID, "portfolio", since="2026-10-11T10:00:01Z")).run is None
    assert s.github.run_queries[-1] == "2026-10-11T10:00:01Z"

    assert status.target == PublishTarget.NETLIFY
    assert status.site_url == "https://octocat-portfolio.netlify.app"
    assert status.run is not None and status.run.conclusion == "success"


async def test_publishes_exactly_the_saved_session() -> None:
    s = Setup()
    first = await s.save(PORTFOLIO, {"src/index.css": "a { color: red }\n"})
    await s.save({**PORTFOLIO, "skills": ["Rust"]}, {"src/index.css": "a { color: blue }\n"})

    # Another device saved since: the browser syncs again before publishing.
    with pytest.raises(WorkspaceConflictError) as conflict:
        await s.service.publish(
            USER, PROJECT_ID, PublishRequest.model_validate({"target": "netlify", "stateSha": first})
        )
    assert conflict.value.details == {"sha": s.sha}

    await s.service.publish(USER, PROJECT_ID, PublishRequest.model_validate({"target": "netlify", "stateSha": s.sha}))
    files = s.files(SITE_REPO)
    assert files["src/index.css"] == "a { color: blue }\n"
    assert '"Rust"' in files["src/content/portfolio.ts"]


async def test_publishing_needs_a_saved_session_with_a_template() -> None:
    s = Setup()
    with pytest.raises(WorkspaceSessionNotFoundError):
        await s.service.publish(
            USER, PROJECT_ID, PublishRequest.model_validate({"target": "netlify", "stateSha": "a" * 40})
        )

    await s.save(None, {})
    with pytest.raises(PublishInvalidError):
        await s.service.publish(
            USER, PROJECT_ID, PublishRequest.model_validate({"target": "netlify", "stateSha": s.sha})
        )


async def test_a_saved_state_listing_a_missing_file_isnt_published() -> None:
    s = Setup()
    await s.save(PORTFOLIO, {"src/index.css": "x"})
    s.github.edit_on_github("octocat/makable-workspace", f"projects/{PROJECT_ID}/files/minimal/src/index.css", None)
    # The state didn't change, so its SHA is still the one the browser has.
    with pytest.raises(WorkspacePartNotFoundError):
        await s.service.publish(
            USER, PROJECT_ID, PublishRequest.model_validate({"target": "netlify", "stateSha": s.sha})
        )
