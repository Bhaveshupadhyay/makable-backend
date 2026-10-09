import asyncio
from typing import Any

import pytest
from fastapi import Request
from fastapi.testclient import TestClient

from app.api.dependencies import get_model_client
from app.api.disconnect import ClientDisconnectedError, cancel_on_disconnect
from app.core.config import Settings
from app.tests.ai_edit_fixtures import GOOD_EDITS, GOOD_REPLY, request_body
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


def test_answers_tier_1_edits_in_the_envelope_without_debug(client: TestClient, model: FakeModelClient) -> None:
    sign_in(client)
    model.replies.append(GOOD_REPLY)

    res = client.post(EDIT, json=request_body())

    assert res.status_code == 200
    assert res.json() == {"success": True, "data": {"tier": 1, "summary": "Made it green.", "edits": GOOD_EDITS}}


def test_adds_debug_when_ai_debug_is_on(client: TestClient, model: FakeModelClient, settings: Settings) -> None:
    settings.ai_debug = True
    sign_in(client)
    model.replies.append('{"escalate": "Needs the navbar."}')

    data = client.post(EDIT, json=request_body()).json()["data"]

    assert data["tier"] == 2
    assert data["reason"] == "Needs the navbar."
    assert data["debug"]["attempts"] == 1
    assert data["debug"]["model"] == "fake-model"
    assert "<instruction>" in data["debug"]["modelInput"]


@pytest.mark.parametrize(
    "overrides",
    [
        {"instruction": "   "},
        {"files": [{"path": "../secrets", "content": "", "reason": ""}]},
        {"fileTree": ["/etc/passwd"]},
        {"history": [{"instruction": "x", "target": None, "reply": "y"}] * 11},
        {"history": [{"instruction": "x", "target": None, "reply": "y" * 1001}]},
        {"template": {"id": "Bad Id", "name": "x", "kind": "react", "version": 1, "contentPath": "a.ts"}},
    ],
)
def test_rejects_invalid_requests(client: TestClient, model: FakeModelClient, overrides: dict[str, Any]) -> None:
    sign_in(client)

    res = client.post(EDIT, json=request_body(**overrides))

    assert res.status_code == 422
    assert model.calls == []


def test_a_rejected_answer_twice_is_a_502(client: TestClient, model: FakeModelClient) -> None:
    sign_in(client)
    model.replies += ["not json", "still not json"]

    res = client.post(EDIT, json=request_body())

    assert res.status_code == 502
    assert res.json()["error"]["code"] == "ai_edit_rejected"


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
