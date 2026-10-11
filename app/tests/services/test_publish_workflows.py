from typing import Any

import pytest
import yaml

from app.constants.publish import TARGET_SECRETS, PublishTarget
from app.services.publish_workflows import render_workflow


def workflow(target: PublishTarget, kind: str = "react", **over: Any) -> Any:
    args: dict[str, Any] = {"branch": "main", "base_path": "/", "project_name": "octocat-portfolio"}
    if target == PublishTarget.GITHUB_PAGES:
        args["project_name"] = None
    return yaml.safe_load(render_workflow(target, kind, **(args | over)))  # type: ignore[arg-type]


def steps(job: dict[str, Any]) -> str:
    """Every step's action, inputs and script, flattened, to look for commands."""
    return "\n".join(str(value) for step in job["steps"] for value in step.values())


def test_github_pages_builds_and_deploys_with_pages_permissions() -> None:
    wf = workflow(PublishTarget.GITHUB_PAGES, base_path="/site/")

    # YAML reads the `on` key as True. Only started runs: a push never deploys.
    assert wf[True] == {"workflow_dispatch": None}
    assert wf["permissions"] == {"contents": "read", "pages": "write", "id-token": "write"}
    assert wf["env"] == {"BRANCH": "main", "BASE_PATH": "/site/"}
    job = wf["jobs"]["deploy"]
    assert job["environment"]["url"] == "${{ steps.deployment.outputs.page_url }}"
    assert 'npx vite build --base "$BASE_PATH"' in steps(job)
    assert "actions/deploy-pages@v4" in steps(job)
    assert set(wf["jobs"]) == {"deploy"}


@pytest.mark.parametrize("target", list(PublishTarget))
def test_no_workflow_deploys_on_push(target: PublishTarget) -> None:
    assert workflow(target)[True] == {"workflow_dispatch": None}


@pytest.mark.parametrize("target", [PublishTarget.VERCEL, PublishTarget.CLOUDFLARE_PAGES, PublishTarget.NETLIFY])
def test_token_platforms_skip_the_deploy_until_their_secrets_exist(target: PublishTarget) -> None:
    wf = workflow(target)

    assert wf["permissions"] == {"contents": "read"}
    assert wf["env"]["PROJECT_NAME"] == "octocat-portfolio"
    check = wf["jobs"]["check"]["steps"][0]
    assert check["env"] == {name: f"${{{{ secrets.{name} }}}}" for name in TARGET_SECRETS[target]}
    for name in TARGET_SECRETS[target]:
        assert f'[ -n "${name}" ] || missing="$missing {name}"' in check["run"]
    deploy = wf["jobs"]["deploy"]
    assert deploy["needs"] == "check" and deploy["if"] == "needs.check.outputs.ready == 'true'"
    # The project is found or created by name in the workflow, from the env variable.
    assert '"$PROJECT_NAME' in steps(deploy)


def test_each_platform_deploys_with_its_own_tool() -> None:
    assert "vercel@latest deploy dist --prod" in steps(workflow(PublishTarget.VERCEL)["jobs"]["deploy"])
    cloudflare = steps(workflow(PublishTarget.CLOUDFLARE_PAGES)["jobs"]["deploy"])
    assert "cloudflare/wrangler-action@v3" in cloudflare
    assert "pages deploy dist --project-name=${{ env.PROJECT_NAME }} --branch=${{ env.BRANCH }}" in cloudflare
    assert "netlify-cli@latest deploy --prod --dir dist --no-build" in steps(
        workflow(PublishTarget.NETLIFY)["jobs"]["deploy"]
    )


def test_a_static_site_is_copied_without_makables_files() -> None:
    job = workflow(PublishTarget.NETLIFY, kind="static")["jobs"]["deploy"]

    assert "find . -mindepth 1 -maxdepth 1 ! -name '.*' ! -name dist -exec cp -R {} dist/ \\;" in steps(job)
    assert "npm install" not in steps(job)


def test_values_stay_data() -> None:
    # Not a branch name makable accepts, but even so it can't add YAML or a command.
    wf = workflow(PublishTarget.VERCEL, branch='x"\n  evil: true', project_name="a")

    assert wf["env"]["BRANCH"] == 'x"\n  evil: true'
    assert "evil" not in wf
