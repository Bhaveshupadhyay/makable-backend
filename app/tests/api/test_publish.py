from typing import Any

from fastapi.testclient import TestClient

from app.tests.api.test_auth import sign_in
from app.tests.fakes import GITHUB_TOKEN, VALID_PLATFORM_TOKEN, FakeGithubClient
from app.tests.session_fixtures import PROJECT_ID, save_body

PUBLISH = f"/api/v1/publish/{PROJECT_ID}"


def body(**over: Any) -> dict[str, Any]:
    return {"target": "vercel", "stateSha": "a" * 40, **over}


def save(client: TestClient) -> str:
    """What the SPA's Publish does first: save the session to the workspace repo."""
    res = client.put(f"/api/v1/workspace/sessions/{PROJECT_ID}", json=save_body())
    assert res.status_code == 200
    sha: str = res.json()["data"]["sha"]
    return sha


def test_needs_a_signed_in_user(client: TestClient) -> None:
    assert client.post(PUBLISH, json=body()).status_code == 401
    assert client.get(f"{PUBLISH}/status", params={"repoName": "portfolio"}).status_code == 401


def test_lists_the_platforms_and_their_secrets(client: TestClient) -> None:
    targets = client.get("/api/v1/publish/targets").json()["data"]

    assert [t["id"] for t in targets] == ["github-pages", "vercel", "cloudflare-pages", "netlify"]
    assert targets[2]["secrets"] == ["CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID"]
    assert targets[1]["tokenUrl"] == "https://vercel.com/account/tokens"


def test_publish_with_a_token_from_the_chat(client: TestClient, github: FakeGithubClient) -> None:
    sign_in(client)
    sha = save(client)

    res = client.post(PUBLISH, json=body(stateSha=sha, credentials={"token": VALID_PLATFORM_TOKEN}))

    assert res.status_code == 200
    data = res.json()["data"]
    assert data["deploy"] == "started" and data["changed"] is True and data["deployStartedAt"]
    assert github.dispatches == [("octocat/portfolio", "makable-deploy.yml", "main")]
    assert data["repoFullName"] == "octocat/portfolio"
    assert data["missingSecrets"] == []
    assert VALID_PLATFORM_TOKEN not in res.text  # never sent back
    assert github.secrets["octocat/portfolio"] == {"VERCEL_TOKEN": VALID_PLATFORM_TOKEN}
    assert GITHUB_TOKEN in github.tokens_seen


def test_publish_then_add_the_secrets_on_github_and_deploy(client: TestClient, github: FakeGithubClient) -> None:
    sign_in(client)
    sha = save(client)
    data = client.post(PUBLISH, json=body(stateSha=sha, target="netlify")).json()["data"]
    assert data["deploy"] == "waiting_for_secrets" and data["missingSecrets"] == ["NETLIFY_AUTH_TOKEN"]

    missing = client.post(f"{PUBLISH}/deploy", json={"repoName": "portfolio"})
    assert missing.status_code == 409
    assert missing.json()["error"]["details"]["missing"] == ["NETLIFY_AUTH_TOKEN"]

    github.secrets["octocat/portfolio"] = {"NETLIFY_AUTH_TOKEN": "t"}
    assert client.post(f"{PUBLISH}/deploy", json={"repoName": "portfolio"}).status_code == 200
    status = client.get(f"{PUBLISH}/status", params={"repoName": "portfolio"}).json()["data"]
    assert status == {"target": "netlify", "siteUrl": "https://octocat-portfolio.netlify.app", "run": None}
    started = client.post(f"{PUBLISH}/deploy", json={"repoName": "portfolio"}).json()["data"]["startedAt"]
    since = client.get(f"{PUBLISH}/status", params={"repoName": "portfolio", "since": started})
    assert since.status_code == 200 and github.run_queries[-1] == started
    bad = client.get(f"{PUBLISH}/status", params={"repoName": "portfolio", "since": "yesterday"})
    assert bad.status_code == 422


def test_a_change_on_github_is_a_409_with_the_head_to_confirm(client: TestClient, github: FakeGithubClient) -> None:
    sign_in(client)
    sha = save(client)
    client.post(PUBLISH, json=body(stateSha=sha, target="github-pages"))
    github.edit_on_github("octocat/octocat.github.io", "src/App.tsx", "mine")

    res = client.post(PUBLISH, json=body(stateSha=sha, target="github-pages"))

    assert res.status_code == 409
    error = res.json()["error"]
    assert error["code"] == "publish_changed_on_github"
    confirmed = client.post(
        PUBLISH, json=body(stateSha=sha, target="github-pages", confirmOverwrite=error["details"]["headSha"])
    )
    assert confirmed.status_code == 200


def test_errors_reach_the_spa_as_envelopes(client: TestClient, github: FakeGithubClient) -> None:
    sign_in(client)
    sha = save(client)
    cases = [
        (body(stateSha=sha, target="github-pages", credentials={"token": "x"}), 422, "validation_error"),
        (body(stateSha=sha, credentials={"token": "wrong"}), 422, "platform_token_invalid"),
        (body(stateSha=sha, repoName=".."), 422, "validation_error"),
        (body(), 409, "workspace_conflict"),  # not the saved state
    ]
    for request, code, error in cases:
        res = client.post(PUBLISH, json=request)
        assert (res.status_code, res.json()["error"]["code"]) == (code, error)

    github.scopes = {"repo"}
    res = client.post(PUBLISH, json=body(stateSha=sha))
    assert (res.status_code, res.json()["error"]["code"]) == (403, "github_missing_scope")


def test_publishes_are_rate_limited_per_user(client: TestClient) -> None:
    sign_in(client)
    sha = save(client)
    codes = [client.post(PUBLISH, json=body(stateSha=sha, target="netlify")).status_code for _ in range(6)]
    assert codes == [200] * 5 + [429]
