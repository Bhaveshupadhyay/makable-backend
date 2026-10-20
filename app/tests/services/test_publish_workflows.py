import os
import subprocess
import sys
from pathlib import Path
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


@pytest.mark.parametrize("target", list(PublishTarget))
@pytest.mark.parametrize("kind", ["react", "static"])
def test_a_linked_site_s_pages_get_the_google_tag_after_the_build(target: PublishTarget, kind: str) -> None:
    wf = workflow(target, kind=kind, measurement_id="G-ABCD1234")

    assert wf["env"]["GA_MEASUREMENT_ID"] == "G-ABCD1234"
    names = [step.get("name") for step in wf["jobs"]["deploy"]["steps"]]
    tag = names.index("Add the Google Analytics tag")
    built = names.index("Serve unknown paths with the app" if kind == "react" else "Collect the site's files")
    assert built < tag
    script = wf["jobs"]["deploy"]["steps"][tag]["run"]
    assert 'os.environ["GA_MEASUREMENT_ID"]' in script and 'pathlib.Path("dist").rglob("*.html")' in script


def test_without_a_stream_there_s_no_tag() -> None:
    wf = workflow(PublishTarget.VERCEL)
    assert "GA_MEASUREMENT_ID" not in wf["env"]
    assert "googletagmanager" not in steps(wf["jobs"]["deploy"])


def test_a_measurement_id_is_checked() -> None:
    with pytest.raises(ValueError, match="measurement id"):
        workflow(PublishTarget.VERCEL, measurement_id='G-1"\n  evil: true')


MEASUREMENT_ID = "G-ABCD1234"
LOADER = f'<script async src="https://www.googletagmanager.com/gtag/js?id={MEASUREMENT_ID}"></script>'
SETUP = "<script>window.dataLayer=window.dataLayer||[];function gtag(){dataLayer.push(arguments);}"
TAG = f"{LOADER}{SETUP}gtag('js',new Date());gtag('config','{MEASUREMENT_ID}');</script>"


def run_tag_step(root: Path, pages: dict[str, str]) -> dict[str, str]:
    """Runs the workflow's tag step on these built pages, like the runner does, and returns them after."""
    wf = workflow(PublishTarget.VERCEL, measurement_id=MEASUREMENT_ID)
    step = next(s for s in wf["jobs"]["deploy"]["steps"] if s.get("name") == "Add the Google Analytics tag")
    script = step["run"].split("<<'PY'\n", 1)[1].rsplit("PY\n", 1)[0]
    for name, html in pages.items():
        (root / "dist" / name).parent.mkdir(parents=True, exist_ok=True)
        (root / "dist" / name).write_text(html, encoding="utf-8")
    env = os.environ | {"GA_MEASUREMENT_ID": MEASUREMENT_ID}
    subprocess.run([sys.executable, "-c", script], cwd=root, env=env, check=True, capture_output=True)  # noqa: S603
    return {name: (root / "dist" / name).read_text(encoding="utf-8") for name in pages}


def test_the_tag_goes_at_the_top_of_every_page_s_head_once(tmp_path: Path) -> None:
    page = '<!doctype html><html><head lang="en"><title>A</title></head><body></body></html>'

    tagged = run_tag_step(tmp_path, {"index.html": page, "blog/post.html": page})

    assert tagged["index.html"] == page.replace('<head lang="en">', f'<head lang="en">{TAG}')
    assert tagged["blog/post.html"] == tagged["index.html"]
    # Publishing again doesn't tag the pages twice.
    assert run_tag_step(tmp_path, tagged) == tagged


def test_a_page_without_a_head_keeps_its_doctype_first(tmp_path: Path) -> None:
    pages = {"a.html": "<!DOCTYPE html>\n<title>A</title><p>Hi", "b.html": "<html lang=en><p>Hi", "c.html": "<p>Hi"}

    tagged = run_tag_step(tmp_path, pages)

    assert tagged == {
        "a.html": f"<!DOCTYPE html>{TAG}\n<title>A</title><p>Hi",
        "b.html": f"<html lang=en>{TAG}<p>Hi",
        "c.html": f"{TAG}<p>Hi",
    }


def test_a_page_with_its_own_gtag_also_reports_to_the_linked_stream(tmp_path: Path) -> None:
    own = (
        '<script async src="https://www.googletagmanager.com/gtag/js?id=G-OTHER999"></script>'
        "<script>window.dataLayer = window.dataLayer || [];function gtag(){dataLayer.push(arguments);}"
        "gtag('js', new Date());gtag('config', 'G-OTHER999');</script>"
    )
    linked = own.replace("G-OTHER999", MEASUREMENT_ID)
    pages = {
        "other.html": f"<html><head>{own}</head><body></body></html>",
        "no-head.html": f"<body>{own}</body>",
        "linked.html": f"<html><head>{linked}</head></html>",
    }

    tagged = run_tag_step(tmp_path, pages)

    # Its own setup stays, and only this stream's config is added after it (gtag.js is loaded once).
    config = f"{SETUP}gtag('config','{MEASUREMENT_ID}');</script>"
    assert tagged["other.html"] == f"<html><head>{own}{config}</head><body></body></html>"
    assert tagged["no-head.html"] == f"<body>{own}{config}</body>"
    # A page that already reports to the linked stream is left alone.
    assert tagged["linked.html"] == pages["linked.html"]
    assert run_tag_step(tmp_path, tagged) == tagged
