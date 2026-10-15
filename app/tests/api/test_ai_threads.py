import pytest
from fastapi.testclient import TestClient

from app.api.dependencies import get_model_client
from app.core.lifespan import refuse_tracing
from app.tests.ai_edit_fixtures import GOOD_REPLY, PROJECT_ID, create_site, request_body
from app.tests.api.test_auth import sign_in
from app.tests.fakes import FakeModelClient

EDIT = "/api/v1/ai/edit"
THREAD = f"/api/v1/ai/threads/{PROJECT_ID}"


@pytest.fixture
def model(client: TestClient) -> FakeModelClient:
    fake = FakeModelClient()
    client.app.dependency_overrides[get_model_client] = lambda: fake  # type: ignore[attr-defined]
    return fake


def test_the_thread_needs_a_signed_in_user(client: TestClient) -> None:
    assert client.get(THREAD).status_code == 401
    assert client.delete(THREAD).status_code == 401


def test_a_site_with_no_requests_has_an_empty_chat(client: TestClient) -> None:
    sign_in(client)

    res = client.get(THREAD)

    assert res.status_code == 200
    assert res.json() == {"success": True, "data": {"projectId": str(PROJECT_ID), "status": "idle", "turns": []}}


def test_the_chat_comes_from_the_server_and_start_over_clears_it(client: TestClient, model: FakeModelClient) -> None:
    sign_in(client)
    base = create_site(client)
    model.replies += [GOOD_REPLY, '{"escalate": "Needs the navbar."}']
    applied = client.post(EDIT, json=request_body(baseCommit=base))
    assert applied.status_code == 200
    head = applied.json()["data"]["commitSha"]
    assert (
        client.post(EDIT, json=request_body(instruction="Make it bigger", target=None, baseCommit=head)).status_code
        == 200
    )

    turns = client.get(THREAD).json()["data"]["turns"]

    assert [(t["instruction"], t["target"], t["outcome"], t["reply"]) for t in turns] == [
        ("Make the contact button green", 'Ada > <a> "Contact"', "applied", "Made it green."),
        ("Make it bigger", None, "escalated", "Not done: it needs a deeper edit (Needs the navbar.)."),
    ]
    assert all(t["createdAt"] for t in turns)

    assert client.delete(THREAD).status_code == 204
    assert client.get(THREAD).json()["data"]["turns"] == []
    assert client.delete(THREAD).status_code == 204
    # The site is forgotten (its repo stays on GitHub).
    assert client.get(f"/api/v1/sites/{PROJECT_ID}").status_code == 404


def test_an_edit_needs_the_project_id(client: TestClient, model: FakeModelClient) -> None:
    sign_in(client)
    body = request_body()
    del body["projectId"]

    res = client.post(EDIT, json=body)

    assert res.status_code == 422
    assert model.calls == []


def test_a_bad_project_id_is_rejected(client: TestClient) -> None:
    sign_in(client)

    assert client.get("/api/v1/ai/threads/not-a-uuid").status_code == 422


def test_the_app_wont_start_with_langsmith_tracing(monkeypatch: pytest.MonkeyPatch) -> None:
    refuse_tracing()
    monkeypatch.setenv("LANGSMITH_TRACING", "true")

    with pytest.raises(RuntimeError, match="tracing"):
        refuse_tracing()
