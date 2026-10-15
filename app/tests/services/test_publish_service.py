"""Publishing a site from its repo, on the fake GitHub: the deploy workflow and the publish settings are committed on
the site's commit when they change, the platform is prepared from a pasted token, and the deploy is started."""

import json
from datetime import UTC, datetime
from typing import Any

import pytest
import yaml
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.deploy_platforms import PlatformTokenInvalidError
from app.clients.github import GithubPagesUnavailableError
from app.constants.publish import WORKFLOW_PATH, PublishTarget
from app.constants.site import MANIFEST_PATH
from app.models.site import Site
from app.schemas.github import GithubWorkflowRun
from app.schemas.publish import DeployRequest, PublishRequest
from app.services.publish_service import (
    PublishDeployNotStartedError,
    PublishInvalidError,
    PublishMissingScopeError,
    PublishNotPublishedError,
    PublishPagesPrivateError,
    PublishRepoArchivedError,
    PublishSecretsMissingError,
    PublishService,
)
from app.services.site_service import VERIFIED_SITES, SiteConflictError
from app.tests.ai_edit_fixtures import PROJECT_ID, REPO, SITE_FILES, USER
from app.tests.conftest import make_site_service
from app.tests.fakes import VALID_PLATFORM_TOKEN, FakeDeployPlatforms, FakeGithubClient
from app.utils.site_scaffold import manifest_file

NOW = datetime(2026, 10, 11, 13, 45, 7, 512000, tzinfo=UTC)
TOKEN = {"token": VALID_PLATFORM_TOKEN}


async def no_sleep(_: float) -> None:
    return None


@pytest.fixture
def platforms() -> FakeDeployPlatforms:
    return FakeDeployPlatforms()


@pytest.fixture
def publisher(
    session: AsyncSession, github: FakeGithubClient, platforms: FakeDeployPlatforms, site: str
) -> PublishService:
    return PublishService(
        github,
        make_site_service(session, github),
        platforms,
        sleep=no_sleep,
        dispatch_delays=(0.1, 0.1),
        clock=lambda: NOW,
    )


def head(github: FakeGithubClient) -> str:
    return github.heads[REPO].commit_sha


async def publish(publisher: PublishService, github: FakeGithubClient, **body: Any) -> Any:
    request = PublishRequest.model_validate({"target": "vercel", "baseCommit": head(github), **body})
    return await publisher.publish(USER, PROJECT_ID, request)


def workflow(github: FakeGithubClient) -> Any:
    return yaml.safe_load(github.files[REPO][WORKFLOW_PATH].content)


def manifest(github: FakeGithubClient) -> Any:
    return json.loads(github.files[REPO][MANIFEST_PATH].content)


async def test_a_first_publish_commits_the_workflow_sets_the_secret_and_deploys(
    publisher: PublishService, github: FakeGithubClient, platforms: FakeDeployPlatforms, site: str
) -> None:
    result = await publish(publisher, github, credentials=TOKEN)

    assert result.changed
    assert result.commit_sha == head(github) != site
    assert github.commits[-1] == (REPO, "Publish to Vercel", [WORKFLOW_PATH, MANIFEST_PATH])
    assert (result.project_name, result.site_url) == ("octocat-my-site", "https://octocat-my-site-1a2b.example.app")
    assert platforms.prepared == [(PublishTarget.VERCEL, "octocat-my-site", "main", None)]
    assert github.secrets[REPO] == {"VERCEL_TOKEN": VALID_PLATFORM_TOKEN}
    assert github.dispatches == [(REPO, "makable-deploy.yml", "main")]
    assert (result.deploy, result.deploy_started_at) == ("started", "2026-10-11T13:44:57Z")
    # YAML 1.1 reads the key `on` as True.
    assert workflow(github)[True] == {"workflow_dispatch": None}
    assert workflow(github)["env"]["PROJECT_NAME"] == "octocat-my-site"
    published = manifest(github)
    assert published["projectId"] == str(PROJECT_ID)
    assert published["publish"] == {
        "target": "vercel",
        "projectName": "octocat-my-site",
        "siteUrl": "https://octocat-my-site-1a2b.example.app",
    }
    # The site's own files aren't touched.
    assert github.files[REPO]["src/App.tsx"].content == SITE_FILES["src/App.tsx"]


async def test_publishing_again_with_nothing_new_deploys_without_a_commit(
    publisher: PublishService, github: FakeGithubClient
) -> None:
    first = await publish(publisher, github, credentials=TOKEN)
    commits = len(github.commits)

    again = await publish(publisher, github)

    assert (again.changed, again.commit_sha) == (False, first.commit_sha)
    assert len(github.commits) == commits
    assert again.project_name == "octocat-my-site"
    assert again.site_url == first.site_url
    assert len(github.dispatches) == 2


async def test_without_a_token_the_deploy_waits_for_the_secrets(
    publisher: PublishService, github: FakeGithubClient
) -> None:
    result = await publish(publisher, github, target="netlify")

    assert (result.deploy, result.missing_secrets) == ("waiting_for_secrets", ["NETLIFY_AUTH_TOKEN"])
    assert github.dispatches == []
    with pytest.raises(PublishSecretsMissingError) as missing:
        await publisher.deploy(USER, PROJECT_ID, DeployRequest())
    assert missing.value.details["missing"] == ["NETLIFY_AUTH_TOKEN"]

    github.secrets[REPO] = {"NETLIFY_AUTH_TOKEN": "added on GitHub"}
    started = await publisher.deploy(USER, PROJECT_ID, DeployRequest())

    assert started.started_at == "2026-10-11T13:44:57Z"
    assert github.dispatches == [(REPO, "makable-deploy.yml", "main")]


async def test_github_pages_needs_the_repo_made_public_first(
    publisher: PublishService, github: FakeGithubClient
) -> None:
    with pytest.raises(PublishPagesPrivateError):
        await publish(publisher, github, target="github-pages")
    assert github.repos[REPO].private

    result = await publish(publisher, github, target="github-pages", makePublic=True)

    assert not github.repos[REPO].private
    assert REPO in github.pages
    assert result.site_url == "https://octocat.github.io/my-site/"
    assert workflow(github)["env"]["BASE_PATH"] == "/my-site/"
    assert "PROJECT_NAME" not in workflow(github)["env"]


async def test_a_repo_made_public_for_pages_is_private_again_if_pages_cant_be_turned_on(
    publisher: PublishService, github: FakeGithubClient
) -> None:
    github.pages_unavailable = True

    with pytest.raises(GithubPagesUnavailableError):
        await publish(publisher, github, target="github-pages", makePublic=True)

    assert github.repos[REPO].private
    assert github.commits == []


async def test_a_plain_html_site_is_deployed_as_it_is(
    session: AsyncSession, github: FakeGithubClient, platforms: FakeDeployPlatforms
) -> None:
    github.add_repo("my-site", private=True, files={**manifest_file(PROJECT_ID), "index.html": "<h1>Hi</h1>\n"})
    session.add(Site(id=PROJECT_ID, user_id=USER.id, name="My site", repo_name="my-site"))
    await session.commit()
    service = PublishService(github, make_site_service(session, github), platforms, sleep=no_sleep, clock=lambda: NOW)

    await publish(service, github, credentials=TOKEN)

    steps = json.dumps(workflow(github)["jobs"])
    assert "npm" not in steps


async def test_a_site_changed_elsewhere_is_a_conflict(
    publisher: PublishService, github: FakeGithubClient, site: str
) -> None:
    github.edit_on_github(REPO, "src/index.css", "body {}\n")

    with pytest.raises(SiteConflictError):
        await publisher.publish(
            USER, PROJECT_ID, PublishRequest.model_validate({"target": "vercel", "baseCommit": site})
        )
    assert github.dispatches == []


async def test_a_token_without_the_workflow_scope_must_reconnect(
    publisher: PublishService, github: FakeGithubClient
) -> None:
    github.scopes = {"repo"}

    with pytest.raises(PublishMissingScopeError):
        await publish(publisher, github)


async def test_an_archived_repo_or_a_refused_token_is_refused(
    publisher: PublishService, github: FakeGithubClient
) -> None:
    with pytest.raises(PlatformTokenInvalidError):
        await publish(publisher, github, credentials={"token": "wrong"})
    assert github.commits == []

    github.repos[REPO] = github.repos[REPO].model_copy(update={"archived": True})
    VERIFIED_SITES.clear()
    with pytest.raises(PublishRepoArchivedError):
        await publish(publisher, github)


async def test_github_takes_a_moment_to_register_a_new_workflow(
    publisher: PublishService, github: FakeGithubClient
) -> None:
    github.dispatch_not_ready = 2
    assert (await publish(publisher, github, credentials=TOKEN)).deploy == "started"

    github.dispatch_not_ready = 3
    with pytest.raises(PublishDeployNotStartedError) as caught:
        await publish(publisher, github)
    assert caught.value.details["actionsUrl"].endswith("/actions/workflows/makable-deploy.yml")


async def test_the_status_reports_the_latest_run_since_a_deploy(
    publisher: PublishService, github: FakeGithubClient
) -> None:
    with pytest.raises(PublishNotPublishedError):
        await publisher.status(USER, PROJECT_ID)
    result = await publish(publisher, github, credentials=TOKEN)
    github.runs[REPO] = GithubWorkflowRun(
        status="completed",
        conclusion="success",
        head_sha=result.commit_sha,
        html_url="https://github.com/run/1",
        created_at="2026-10-11T13:45:09Z",
    )

    found = await publisher.status(USER, PROJECT_ID, since=result.deploy_started_at)

    assert found.run is not None and found.run.conclusion == "success"
    assert found.site_url == result.site_url
    assert (await publisher.status(USER, PROJECT_ID, since="2026-10-11T14:00:00Z")).run is None


def test_github_pages_takes_no_token() -> None:
    with pytest.raises(ValueError, match="no token"):
        PublishRequest.model_validate({"target": "github-pages", "baseCommit": "a" * 40, "credentials": TOKEN})
    assert PublishInvalidError.code == "publish_invalid"
