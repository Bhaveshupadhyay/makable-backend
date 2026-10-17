"""Publishing a site: its repo gets a deploy workflow for the platform the user picked, and makable starts it."""

from typing import Annotated, Literal, Self

from pydantic import Field, SecretStr, model_validator

from app.constants.publish import MAX_TOKEN_CHARS, PROJECT_NAME, PublishTarget
from app.schemas.common import CamelModel
from app.schemas.site import CommitSha

ProjectName = Annotated[str, Field(pattern=f"^{PROJECT_NAME.pattern}$")]


class PublishCredentials(CamelModel):
    """A platform token the user pasted in the chat. It's checked, used to set the project up, and saved as an
    Actions secret in the site repo. makable never stores it or sends it back."""

    token: Annotated[SecretStr, Field(min_length=1, max_length=MAX_TOKEN_CHARS)]
    # Cloudflare only: which account, when the token can see several.
    account_id: Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")] | None = None


class PublishRequest(CamelModel):
    """Publishes the site from its repo, at `base_commit` (the SPA saves the user's edits first). The deploy workflow
    (and the manifest's publish settings) are committed on it when they change."""

    target: PublishTarget
    base_commit: CommitSha
    # The platform project (not for GitHub Pages). Defaults to the one published before, else `<login>-<repo>`.
    project_name: ProjectName | None = None
    # None: the user adds the Actions secrets on GitHub themselves.
    credentials: PublishCredentials | None = None
    # GitHub Pages needs a public repo on GitHub's free plan: the user agreed to make the site's repo public.
    make_public: bool = False

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.target == PublishTarget.GITHUB_PAGES and (
            self.credentials is not None or self.project_name is not None
        ):
            raise ValueError("GitHub Pages takes no token or project name")
        return self


DeployState = Literal["started", "waiting_for_secrets"]


class PublishResult(CamelModel):
    target: PublishTarget
    repo_full_name: str
    repo_url: str
    branch: str
    # The commit the site is at now (the workflow's commit, when this publish made one): the SPA builds on it.
    commit_sha: str
    # Whether this publish committed anything (the workflow or the publish settings). Either way the site is deployed
    # from what's on GitHub.
    changed: bool
    # `started`: makable started the deploy workflow (a push never does). `waiting_for_secrets`: it wasn't started,
    # because the secrets in `missing_secrets` aren't on the repo yet.
    deploy: DeployState
    # When the deploy was started (UTC, to the second): the status check finds this deploy's run with it.
    deploy_started_at: str | None
    # Where the site will be. For a token added on GitHub, the platform's usual address for the project.
    site_url: str | None
    project_name: str | None
    missing_secrets: list[str]
    secrets_url: str
    actions_url: str


class DeployRequest(CamelModel):
    """Runs the deploy workflow again, e.g. after the user added the secrets on GitHub."""

    # Set the platform's secrets from a token pasted in the chat first.
    credentials: PublishCredentials | None = None


class DeployStarted(CamelModel):
    repo_url: str
    actions_url: str
    # When the deploy was started (UTC, to the second), for the status check.
    started_at: str


class WorkflowRun(CamelModel):
    status: str
    # `success`, `failure`, `cancelled`, `skipped`... once `status` is `completed`.
    conclusion: str | None
    head_sha: str
    html_url: str
    created_at: str


class PublishStatus(CamelModel):
    target: PublishTarget
    site_url: str | None
    # The deploy workflow's latest run (since the time asked for), or None if there's none yet.
    run: WorkflowRun | None


class PublishTargetInfo(CamelModel):
    id: PublishTarget
    name: str
    # The Actions secrets the user adds on GitHub if they don't paste a token in the chat.
    secrets: list[str]
    token_url: str | None


class PublishSettings(CamelModel):
    """Where a site is published, kept in its manifest (`.makable/manifest.json`, under `publish`)."""

    target: PublishTarget
    project_name: str | None = None
    site_url: str | None = None
