from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.clients.github import GithubRateLimitedError
from app.tests.api.test_auth import sign_in
from app.tests.fakes import GITHUB_TOKEN, FakeGithubClient
from app.tests.session_fixtures import PROJECT_ID, chunk, save_body, state_body

SESSIONS = "/api/v1/workspace/sessions"


def test_needs_a_signed_in_user(client: TestClient) -> None:
    assert client.get(f"{SESSIONS}/latest").status_code == 401
    assert client.put(f"{SESSIONS}/{PROJECT_ID}", json=save_body()).status_code == 401


def test_save_then_restore_on_another_device(client: TestClient, github: FakeGithubClient) -> None:
    sign_in(client)
    assert client.get(f"{SESSIONS}/latest").json()["error"]["code"] == "workspace_session_not_found"

    saved = client.put(f"{SESSIONS}/{PROJECT_ID}", json=save_body())

    assert saved.status_code == 200
    sha = saved.json()["data"]["sha"]
    assert saved.json()["data"]["repoUrl"] == "https://github.com/octocat/makable-workspace"
    assert GITHUB_TOKEN in github.tokens_seen  # the user's own token wrote it

    # A new device: the newest state (exactly as saved), then each part it lists.
    latest = client.get(f"{SESSIONS}/latest").json()["data"]
    assert latest == {"state": state_body(), "sha": sha}
    part = client.get(f"{SESSIONS}/{PROJECT_ID}/parts/messages/0001.json").json()["data"]
    assert part == {"path": "messages/0001.json", "content": chunk("I want a portfolio", "Use Minimal")}
    file = client.get(f"{SESSIONS}/{PROJECT_ID}/parts/files/minimal/src/index.css").json()["data"]
    assert file["content"] == "a { color: red }\n"
    missing = client.get(f"{SESSIONS}/{PROJECT_ID}/parts/messages/0009.json")
    assert missing.status_code == 404
    assert client.get(f"{SESSIONS}/{PROJECT_ID}/parts/state.json").status_code == 422


def test_a_stale_save_is_a_409_with_the_current_sha(client: TestClient) -> None:
    sign_in(client)
    first = client.put(f"{SESSIONS}/{PROJECT_ID}", json=save_body()).json()["data"]["sha"]
    second = client.put(f"{SESSIONS}/{PROJECT_ID}", json=save_body(first, [], saved_at="2026-10-10T13:00:00.000Z"))

    stale = client.put(f"{SESSIONS}/{PROJECT_ID}", json=save_body(first, []))

    assert stale.status_code == 409
    error = stale.json()["error"]
    assert error["code"] == "workspace_conflict"
    assert error["details"] == {"sha": second.json()["data"]["sha"], "savedAt": "2026-10-10T13:00:00.000Z"}


@pytest.mark.parametrize(
    "body",
    [
        save_body(parts=[{"path": "state.json", "content": "{}"}]),
        save_body(parts=[{"path": "../secrets", "content": "x"}]),
        save_body(parts=[{"path": "files/minimal/../../x", "content": "x"}]),
        save_body(parts=[{"path": "messages/0001.json", "content": "not json"}]),
        save_body(parts=[{"path": "messages/0001.json", "content": '{"messages": []}'}]),
        save_body(parts=[{"path": "messages/0000.json", "content": chunk("x")}]),
        save_body(parts=[{"path": "files/minimal/a.css", "content": "x" * 60_001}]),
        save_body(parts=[{"path": "files/minimal/a.css", "content": "x"}], deletes=["files/minimal/a.css"]),
        save_body(parts=[{"path": f"files/minimal/f{i}.css", "content": "x"} for i in range(21)]),
        save_body(deletes=["state.json"]),
        save_body(parts=[{"path": "ai-history/minimal.json", "content": '{"minimal": []}'}]),
        save_body(parts=[{"path": "ai-history.json", "content": "[]"}]),
        save_body(version=2),
        save_body(savedAt="yesterday"),
    ],
)
def test_rejects_invalid_saves(client: TestClient, github: FakeGithubClient, body: dict[str, Any]) -> None:
    sign_in(client)
    assert client.put(f"{SESSIONS}/{PROJECT_ID}", json=body).status_code == 422
    assert github.commits == []


def test_a_rate_limit_says_how_long_to_wait(client: TestClient, github: FakeGithubClient) -> None:
    sign_in(client)
    first = client.put(f"{SESSIONS}/{PROJECT_ID}", json=save_body()).json()["data"]["sha"]
    github.fail_next_write = GithubRateLimitedError(retry_after=180)

    res = client.put(f"{SESSIONS}/{PROJECT_ID}", json=save_body(first, saved_at="2026-10-10T12:01:00.000Z"))

    assert res.status_code == 429
    assert res.json()["error"]["code"] == "github_rate_limited"
    assert res.json()["error"]["details"] == {"retryAfter": 180}
