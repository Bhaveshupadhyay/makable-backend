from fastapi.testclient import TestClient

from app.clients.github import GithubRateLimitedError
from app.constants.workspace import session_path
from app.tests.api.test_auth import sign_in
from app.tests.fakes import GITHUB_TOKEN, FakeGithubClient
from app.tests.session_fixtures import PROJECT_ID, snapshot_body

SESSIONS = "/api/v1/workspace/sessions"


def test_needs_a_signed_in_user(client: TestClient) -> None:
    assert client.get(f"{SESSIONS}/latest").status_code == 401
    assert client.put(f"{SESSIONS}/{PROJECT_ID}", json={"snapshot": snapshot_body()}).status_code == 401


def test_save_then_restore_on_another_device(client: TestClient, github: FakeGithubClient) -> None:
    sign_in(client)
    assert client.get(f"{SESSIONS}/latest").json()["error"]["code"] == "workspace_session_not_found"

    saved = client.put(f"{SESSIONS}/{PROJECT_ID}", json={"snapshot": snapshot_body(), "baseSha": None})

    assert saved.status_code == 200
    sha = saved.json()["data"]["sha"]
    assert saved.json()["data"]["repoUrl"] == "https://github.com/octocat/makable-workspace"
    # The user's own token wrote it.
    assert GITHUB_TOKEN in github.tokens_seen
    assert session_path(str(PROJECT_ID)) in github.files["octocat/makable-workspace"]

    latest = client.get(f"{SESSIONS}/latest").json()["data"]
    assert latest == {"snapshot": snapshot_body(), "sha": sha}
    assert client.get(f"{SESSIONS}/{PROJECT_ID}").json()["data"]["sha"] == sha


def test_a_stale_save_is_a_409_with_the_current_sha(client: TestClient) -> None:
    sign_in(client)
    first = client.put(f"{SESSIONS}/{PROJECT_ID}", json={"snapshot": snapshot_body()}).json()["data"]["sha"]
    later = snapshot_body(exported_at="2026-10-10T13:00:00.000Z")
    second = client.put(f"{SESSIONS}/{PROJECT_ID}", json={"snapshot": later, "baseSha": first})

    stale = client.put(f"{SESSIONS}/{PROJECT_ID}", json={"snapshot": snapshot_body(), "baseSha": first})

    assert stale.status_code == 409
    error = stale.json()["error"]
    assert error["code"] == "workspace_conflict"
    assert error["details"]["sha"] == second.json()["data"]["sha"]


def test_rejects_invalid_sessions(client: TestClient) -> None:
    sign_in(client)
    bad = [
        snapshot_body(version=2),
        snapshot_body(fileEdits={"minimal": {"../x": "y"}}),
        snapshot_body(exportedAt="yesterday"),
        {**snapshot_body(), "conversation": {**snapshot_body()["conversation"], "messages": []}},
    ]
    for snapshot in bad:
        assert client.put(f"{SESSIONS}/{PROJECT_ID}", json={"snapshot": snapshot}).status_code == 422


def test_a_rate_limit_says_how_long_to_wait(client: TestClient, github: FakeGithubClient) -> None:
    sign_in(client)
    github.fail_next_write = GithubRateLimitedError(retry_after=180)

    res = client.put(f"{SESSIONS}/{PROJECT_ID}", json={"snapshot": snapshot_body()})

    assert res.status_code == 429
    assert res.json()["error"]["code"] == "github_rate_limited"
    assert res.json()["error"]["details"] == {"retryAfter": 180}
