from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.api import dependencies
from app.core.limits import ConcurrencyLimit, PerKeyLimit
from app.tests.api.test_auth import sign_in
from app.tests.session_fixtures import PROJECT_ID, save_body

SESSIONS = "/api/v1/workspace/sessions"


def test_an_oversized_body_is_refused_before_it_is_read(client: TestClient) -> None:
    res = client.post("/api/v1/auth/refresh", content=b"x" * 70_000)

    assert res.status_code == 413
    body = res.json()
    assert body["error"]["code"] == "request_too_large"
    assert body["requestId"] == res.headers["x-request-id"]
    assert res.headers["x-content-type-options"] == "nosniff"


def test_a_streamed_body_is_cut_off_at_the_limit(client: TestClient) -> None:
    def chunks() -> Iterator[bytes]:
        for _ in range(20):
            yield b"x" * 10_000

    res = client.post("/api/v1/auth/refresh", content=chunks())

    assert "content-length" not in res.request.headers
    assert res.status_code == 413


def test_routes_that_need_more_get_their_own_limit(client: TestClient) -> None:
    # 1 MB is fine for an AI edit (six 60,000-character files); it gets as far as the sign-in check.
    assert client.post("/api/v1/ai/edit", content=b"x" * 1_000_000).status_code == 401
    assert client.post("/api/v1/ai/edit", content=b"x" * 2_000_001).status_code == 413


def test_a_full_worker_answers_busy_with_retry_after(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    sign_in(client)
    monkeypatch.setattr(dependencies, "WORKSPACE_SLOTS", ConcurrencyLimit(0))

    res = client.get(f"{SESSIONS}/latest")

    assert res.status_code == 503
    assert res.headers["retry-after"] == "5"
    assert res.json()["error"]["code"] == "service_busy"
    assert res.json()["error"]["details"] == {"retryAfter": 5}


def test_saving_too_often_is_a_429_with_the_wait(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    sign_in(client)
    monkeypatch.setattr(dependencies, "WORKSPACE_SAVES", PerKeyLimit(burst=1, every=60))
    first = client.put(f"{SESSIONS}/{PROJECT_ID}", json=save_body())
    assert first.status_code == 200

    again = client.put(f"{SESSIONS}/{PROJECT_ID}", json=save_body(first.json()["data"]["sha"], []))

    assert again.status_code == 429
    assert again.json()["error"]["code"] == "too_many_requests"
    assert again.json()["error"]["details"]["retryAfter"] == 60
    assert again.headers["retry-after"] == "60"
