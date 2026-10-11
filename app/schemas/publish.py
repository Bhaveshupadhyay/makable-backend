"""Publishing a site to a repo on the user's GitHub, deployed by a workflow to the platform they picked."""

from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import AfterValidator, Field, SecretStr, StringConstraints, model_validator

from app.constants.publish import (
    MAX_TOKEN_CHARS,
    PROJECT_NAME,
    REPO_NAME,
    PublishTarget,
)
from app.schemas.common import CamelModel
from app.schemas.portfolio import TemplateId
from app.schemas.workspace import Sha


def _repo_name(value: str) -> str:
    # Checked with `re`: pydantic's own patterns can't look ahead.
    if not REPO_NAME.fullmatch(value):
        raise ValueError("must be a GitHub repository name")
    return value


RepoName = Annotated[str, AfterValidator(_repo_name)]
ProjectName = Annotated[str, Field(pattern=f"^{PROJECT_NAME.pattern}$")]


class PublishCredentials(CamelModel):
    """A platform token the user pasted in the chat. It's checked, used to set the project up, and saved as an
    Actions secret in the site repo. makable never stores it or sends it back."""

    token: Annotated[SecretStr, Field(min_length=1, max_length=MAX_TOKEN_CHARS)]
    # Cloudflare only: which account, when the token can see several.
    account_id: Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")] | None = None


class PublishRequest(CamelModel):
    """Publishes the site as saved in the user's `makable-workspace` repo: the browser saves first, then sends the
    SHA of the saved state. The server reads the content and the AI-edited files from there."""

    target: PublishTarget
    # The SHA of the site's saved `state.json` (what the save returned). If another device saved since, the
    # answer is 409 `workspace_conflict`.
    state_sha: Sha
    # The site repo. The first publish may leave it out: `<login>.github.io` for GitHub Pages, else `portfolio`.
    repo_name: RepoName | None = None
    # The platform project (not for GitHub Pages). Defaults to the one published before, else `<login>-<repo>`.
    project_name: ProjectName | None = None
    # Only for a repo makable creates. GitHub Pages needs a public repo on GitHub's free plan.
    private: bool | None = None
    # None: the user adds the Actions secrets on GitHub themselves.
    credentials: PublishCredentials | None = None
    # The head commit the user agreed to overwrite after a `publish_changed_on_github` conflict.
    confirm_overwrite: Sha | None = None

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.target == PublishTarget.GITHUB_PAGES:
            if self.credentials is not None or self.project_name is not None:
                raise ValueError("GitHub Pages takes no token or project name")
            if self.private:
                raise ValueError("GitHub Pages needs a public repository")
        return self


DeployState = Literal["started", "waiting_for_secrets", "unchanged"]


class PublishResult(CamelModel):
    target: PublishTarget
    repo_full_name: str
    repo_url: str
    branch: str
    # The commit with the site; the branch's current head when nothing changed.
    commit_sha: str
    # `started`: the push (or a manual run) started the deploy workflow. `waiting_for_secrets`: the workflow skips
    # the deploy until the secrets in `missing_secrets` are added. `unchanged`: nothing to commit or deploy.
    deploy: DeployState
    # Where the site will be. For a token added on GitHub, the platform's usual address for the project.
    site_url: str | None
    project_name: str | None
    missing_secrets: list[str]
    secrets_url: str
    actions_url: str


class DeployRequest(CamelModel):
    """Runs the deploy workflow again, e.g. after the user added the secrets on GitHub."""

    repo_name: RepoName
    # Set the platform's secrets from a token pasted in the chat first.
    credentials: PublishCredentials | None = None


class DeployStarted(CamelModel):
    repo_url: str
    actions_url: str


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
    # The deploy workflow's latest run, or None if it hasn't run yet.
    run: WorkflowRun | None


class PublishTargetInfo(CamelModel):
    id: PublishTarget
    name: str
    # The Actions secrets the user adds on GitHub if they don't paste a token in the chat.
    secrets: list[str]
    token_url: str | None


class SiteManifest(CamelModel):
    """`.makable/manifest.json` in a site repo: marks the repo as makable's and lists the files it wrote."""

    format: Literal["makable-site"]
    version: Literal[1]
    project_id: UUID
    target: PublishTarget
    template_id: TemplateId
    template_version: int
    project_name: str | None = None
    site_url: str | None = None
    # Each managed file's path -> the blob SHA makable wrote.
    files: dict[str, Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{40}$")]]
