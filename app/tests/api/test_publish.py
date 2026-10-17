from typing import Any

from fastapi.testclient import TestClient

from app.tests.ai_edit_fixtures import PROJECT_ID, REPO, create_site
from app.tests.api.test_auth import sign_in
from app.tests.fakes import GITHUB_TOKEN, VALID_PLATFORM_TOKEN, FakeGithubClient

PUBLISH = f"/api/v1/publish/{PROJECT_ID}"


def body(base: str, **over: Any) -> dict[str, Any]:
    return {"target": "vercel", "baseCommit": base, **over}


def test_needs_a_signed_in_user(client: TestClient) -> None:
    assert client.post(PUBLISH, json=body("a" * 40)).status_code == 401
    assert client.get(f"{PUBLISH}/status").status_code == 401


def test_lists_the_platforms_and_their_secrets(client: TestClient) -> None:
    targets = client.get("/api/v1/publish/targets").json()["data"]

    assert [t["id"] for t in targets] == ["github-pages", "vercel", "cloudflare-pages", "netlify"]
    assert targets[2]["secrets"] == ["CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID"]
    assert targets[1]["tokenUrl"] == "https://vercel.com/account/tokens"


def test_publish_with_a_token_from_the_chat(client: TestClient, github: FakeGithubClient) -> None:
    sign_in(client)
    base = create_site(client)

    res = client.post(PUBLISH, json=body(base, credentials={"token": VALID_PLATFORM_TOKEN}))

    assert res.status_code == 200
    data = res.json()["data"]
    assert data["deploy"] == "started" and data["changed"] is True and data["deployStartedAt"]
    assert data["commitSha"] == github.heads[REPO].commit_sha != base
    assert github.dispatches == [(REPO, "makable-deploy.yml", "main")]
    assert data["repoFullName"] == REPO
    assert data["missingSecrets"] == []
    assert VALID_PLATFORM_TOKEN not in res.text  # never sent back
    assert github.secrets[REPO] == {"VERCEL_TOKEN": VALID_PLATFORM_TOKEN}
    assert GITHUB_TOKEN in github.tokens_seen
    # The SPA builds on the publish's commit.
    assert client.get(f"/api/v1/sites/{PROJECT_ID}").json()["data"]["commitSha"] == data["commitSha"]


def test_publish_then_add_the_secrets_on_github_and_deploy(client: TestClient, github: FakeGithubClient) -> None:
    sign_in(client)
    base = create_site(client)
    data = client.post(PUBLISH, json=body(base, target="netlify")).json()["data"]
    assert data["deploy"] == "waiting_for_secrets" and data["missingSecrets"] == ["NETLIFY_AUTH_TOKEN"]

    missing = client.post(f"{PUBLISH}/deploy", json={})
    assert (missing.status_code, missing.json()["error"]["code"]) == (409, "publish_secrets_missing")

    github.secrets[REPO] = {"NETLIFY_AUTH_TOKEN": "x"}
    started = client.post(f"{PUBLISH}/deploy", json={})
    assert started.status_code == 200
    assert started.json()["data"]["startedAt"]

    status = client.get(f"{PUBLISH}/status", params={"since": started.json()["data"]["startedAt"]})
    assert status.status_code == 200
    assert status.json()["data"]["target"] == "netlify"


def test_github_pages_asks_before_making_the_repo_public(client: TestClient, github: FakeGithubClient) -> None:
    sign_in(client)
    base = create_site(client)

    asked = client.post(PUBLISH, json=body(base, target="github-pages"))
    assert (asked.status_code, asked.json()["error"]["code"]) == (409, "publish_pages_private")

    res = client.post(PUBLISH, json=body(base, target="github-pages", makePublic=True))
    assert res.status_code == 200
    assert res.json()["data"]["siteUrl"] == "https://octocat.github.io/my-site/"
    assert not github.repos[REPO].private


def test_an_unpublished_site_has_no_status(client: TestClient) -> None:
    sign_in(client)
    create_site(client)

    res = client.get(f"{PUBLISH}/status")

    assert (res.status_code, res.json()["error"]["code"]) == (404, "publish_not_published")
