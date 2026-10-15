import asyncio
from typing import Any

import pytest
from fastapi import Request
from fastapi.testclient import TestClient

from app.api.dependencies import get_model_client
from app.api.disconnect import ClientDisconnectedError, cancel_on_disconnect
from app.core.config import Settings
from app.tests.ai_edit_fixtures import GOOD_REPLY, GREEN_HERO, HERO_PATH, create_site, request_body
from app.tests.api.test_auth import sign_in
from app.tests.fakes import FakeModelClient

EDIT = "/api/v1/ai/edit"


@pytest.fixture
def model(client: TestClient) -> FakeModelClient:
    fake = FakeModelClient()
    client.app.dependency_overrides[get_model_client] = lambda: fake  # type: ignore[attr-defined]
    return fake


def test_needs_a_signed_in_user(client: TestClient, model: FakeModelClient) -> None:
    res = client.post(EDIT, json=request_body())

    assert res.status_code == 401
    assert res.json()["error"]["code"] == "unauthorized"
    assert model.calls == []


def test_commits_the_change_and_answers_the_changed_files_without_debug(
    client: TestClient, model: FakeModelClient
) -> None:
    sign_in(client)
    base = create_site(client)
    model.replies.append(GOOD_REPLY)

    res = client.post(EDIT, json=request_body(baseCommit=base))

    assert res.status_code == 200
    data = res.json()["data"]
    assert (data["status"], data["summary"], data["files"]) == ("applied", "Made it green.", {HERO_PATH: GREEN_HERO})
    assert data["commitSha"] != base
    assert "debug" not in data
    assert client.get("/api/v1/sites/" + request_body()["projectId"]).json()["data"]["commitSha"] == data["commitSha"]


def test_adds_debug_when_ai_debug_is_on(client: TestClient, model: FakeModelClient, settings: Settings) -> None:
    settings.ai_debug = True
    sign_in(client)
    base = create_site(client)
    model.replies.append('{"escalate": "Needs the navbar."}')

    data = client.post(EDIT, json=request_body(baseCommit=base)).json()["data"]

    assert data["status"] == "escalated"
    assert data["reason"] == "Needs the navbar."
    assert data["debug"]["attempts"] == 1
    assert data["debug"]["model"] == "fake-model"
    assert "<instruction>" in data["debug"]["modelInput"]


@pytest.mark.parametrize(
    "overrides",
    [
        {"instruction": "   "},
        {"baseCommit": "main"},
        {"projectId": "not-a-uuid"},
        {"target": {**request_body()["target"], "source": {"path": "../secrets", "line": 1, "column": 0}}},
        {"screenshot": "https://example.com/a.png"},
        {"screenshot": "data:image/svg+xml;base64,PHN2Zz4="},
    ],
)
def test_rejects_invalid_requests(client: TestClient, model: FakeModelClient, overrides: dict[str, Any]) -> None:
    sign_in(client)

    res = client.post(EDIT, json=request_body(**overrides))

    assert res.status_code == 422
    assert model.calls == []


def test_a_rejected_answer_twice_is_a_502(client: TestClient, model: FakeModelClient) -> None:
    sign_in(client)
    base = create_site(client)
    model.replies += ["not json", "still not json"]

    res = client.post(EDIT, json=request_body(baseCommit=base))

    assert res.status_code == 502
    assert res.json()["error"]["code"] == "ai_edit_rejected"


def test_an_edit_on_a_site_without_its_repo_is_not_found(client: TestClient, model: FakeModelClient) -> None:
    sign_in(client)

    res = client.post(EDIT, json=request_body())

    assert res.status_code == 404
    assert res.json()["error"]["code"] == "site_not_found"


def test_an_edit_on_an_old_commit_is_a_conflict(client: TestClient, model: FakeModelClient) -> None:
    sign_in(client)
    base = create_site(client)
    model.replies.append(GOOD_REPLY)
    assert client.post(EDIT, json=request_body(baseCommit=base)).status_code == 200

    res = client.post(EDIT, json=request_body(baseCommit=base))

    assert res.status_code == 409
    assert res.json()["error"]["code"] == "site_conflict"


def disconnected_request() -> Request:
    async def receive() -> dict[str, Any]:
        return {"type": "http.disconnect"}

    return Request({"type": "http", "method": "POST", "headers": []}, receive)


async def test_the_work_is_cancelled_when_the_client_disconnects() -> None:
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def slow_model_call() -> str:
        started.set()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return "done"

    with pytest.raises(ClientDisconnectedError):
        await cancel_on_disconnect(disconnected_request(), slow_model_call())
    await asyncio.sleep(0)
    assert started.is_set()
    assert cancelled.is_set()


async def test_cancelled_work_gets_to_clean_up_before_the_request_ends() -> None:
    cleaned_up = asyncio.Event()

    async def work_that_records_its_cancellation() -> str:
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            await asyncio.sleep(0.01)  # e.g. writing the `cancelled` row
            cleaned_up.set()
            raise
        return "done"

    with pytest.raises(ClientDisconnectedError):
        await cancel_on_disconnect(disconnected_request(), work_that_records_its_cancellation())
    assert cleaned_up.is_set()
