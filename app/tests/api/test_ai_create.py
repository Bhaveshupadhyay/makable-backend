import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.api.dependencies import get_model_client
from app.tests.api.test_auth import sign_in
from app.tests.fakes import FakeGithubClient, FakeModelClient
from app.tests.services.test_site_create import CSS, HERO, NEW, PROMPT, replies

CREATE = "/api/v1/ai/create"


@pytest.fixture
def model(client: TestClient) -> FakeModelClient:
    fake = FakeModelClient()
    client.app.dependency_overrides[get_model_client] = lambda: fake  # type: ignore[attr-defined]
    return fake


def stream(client: TestClient, url: str, body: dict[str, Any]) -> list[dict[str, Any]]:
    with client.stream("POST", url, json=body) as res:
        assert res.status_code == 200
        assert res.headers["content-type"].startswith("application/x-ndjson")
        assert res.headers["x-accel-buffering"] == "no"
        return [json.loads(line) for line in res.iter_lines() if line]


def test_creating_needs_a_signed_in_user(client: TestClient) -> None:
    res = client.post(CREATE, json={"projectId": str(NEW), "prompt": PROMPT})

    assert res.status_code == 401


def test_a_site_is_created_over_a_stream(client: TestClient, model: FakeModelClient, github: FakeGithubClient) -> None:
    sign_in(client)
    model.replies += replies()

    asked = stream(client, CREATE, {"projectId": str(NEW), "prompt": PROMPT})[-1]
    assert asked["type"] == "result"
    assert asked["data"]["status"] == "needs_input"
    assert client.get(f"/api/v1/ai/threads/{NEW}").json()["data"]["run"]["mode"] == "create"

    events = stream(client, f"{CREATE}/{NEW}/answer", {"runId": asked["data"]["runId"], "answer": "Ada's Bakery"})

    assert [e["stage"] for e in events if e["type"] == "progress"][:2] == ["setup", "plan"]
    created = events[-1]["data"]
    assert created["status"] == "created"
    site = client.get(f"/api/v1/sites/{NEW}").json()["data"]
    assert site["commitSha"] == created["commitSha"]
    assert (site["files"]["src/components/Hero.tsx"], site["files"]["src/index.css"]) == (HERO, CSS)
    turns = client.get(f"/api/v1/ai/threads/{NEW}").json()["data"]["turns"]
    assert [t["instruction"] for t in turns] == [PROMPT]


def test_errors_after_the_stream_starts_are_error_lines(client: TestClient, model: FakeModelClient) -> None:
    sign_in(client)
    model.replies += ["not json", "still not json"]
    asked = stream(client, CREATE, {"projectId": str(NEW), "prompt": PROMPT})[-1]

    events = stream(client, f"{CREATE}/{NEW}/answer", {"runId": asked["data"]["runId"], "answer": "Ada's Bakery"})

    assert events[-1] == {
        "type": "error",
        "error": {
            "code": "ai_create_failed",
            "message": "The AI couldn't build the site. Try again, maybe with another model.",
            "details": None,
        },
    }
