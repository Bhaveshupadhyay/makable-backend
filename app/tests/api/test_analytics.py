from typing import Any
from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient

from app.api.dependencies import get_google_client
from app.constants.analytics import GOOGLE_STATE_COOKIE, GOOGLE_STATE_COOKIE_PATH
from app.constants.publish import WORKFLOW_PATH
from app.tests.ai_edit_fixtures import PROJECT_ID, REPO, create_site
from app.tests.api.test_auth import sign_in
from app.tests.conftest import APP_URL
from app.tests.fakes import (
    GA_MEASUREMENT_ID,
    GA_PROPERTY_ID,
    GA_STREAM_ID,
    GOOGLE_CODE,
    GOOGLE_EMAIL,
    GOOGLE_REFRESH_TOKEN,
    VALID_PLATFORM_TOKEN,
    FakeGithubClient,
    FakeGoogleClient,
)

GOOGLE = "/api/v1/analytics/google"
SITE = f"/api/v1/sites/{PROJECT_ID}/analytics"
LINK = {"propertyId": GA_PROPERTY_ID, "streamId": GA_STREAM_ID}


def start_connect(client: TestClient, return_to: str = "/analytics") -> dict[str, list[str]]:
    res = client.post(f"{GOOGLE}/connect", json={"returnTo": return_to})
    assert res.status_code == 200, res.json()
    return parse_qs(urlsplit(res.json()["data"]["authorizeUrl"]).query)


def callback(client: TestClient, **params: str) -> str:
    res = client.get(f"{GOOGLE}/callback", params=params)
    assert res.status_code == 302
    return res.headers["location"]


def connect(client: TestClient) -> None:
    query = start_connect(client)
    assert callback(client, code=GOOGLE_CODE, state=query["state"][0]) == f"{APP_URL}/analytics?analytics=connected"


def linked_site(client: TestClient) -> str:
    sign_in(client)
    base = create_site(client)
    connect(client)
    assert client.put(SITE, json=LINK).status_code == 200
    return base


def error(res: Any) -> tuple[int, str]:
    return res.status_code, res.json()["error"]["code"]


def test_needs_a_signed_in_user(client: TestClient) -> None:
    assert client.get(GOOGLE).status_code == 401
    assert client.post(f"{GOOGLE}/connect", json={}).status_code == 401
    assert client.get(f"{SITE}/report").status_code == 401


def test_connect_google_pick_a_stream_and_read_the_site_s_traffic(client: TestClient, google: FakeGoogleClient) -> None:
    sign_in(client)
    create_site(client)
    assert client.get(GOOGLE).json()["data"] == {"configured": True, "connected": False, "email": None}

    query = start_connect(client)
    assert query["redirect_uri"] == [f"{APP_URL}/api/v1/analytics/google/callback"]
    # The state cookie is only sent to the callback.
    assert client.cookies.get(GOOGLE_STATE_COOKIE, path=GOOGLE_STATE_COOKIE_PATH)
    assert callback(client, code=GOOGLE_CODE, state=query["state"][0]) == f"{APP_URL}/analytics?analytics=connected"
    assert client.cookies.get(GOOGLE_STATE_COOKIE, path=GOOGLE_STATE_COOKIE_PATH) is None
    assert client.get(GOOGLE).json()["data"] == {"configured": True, "connected": True, "email": GOOGLE_EMAIL}

    accounts = client.get(f"{GOOGLE}/properties").json()["data"]
    assert accounts == [{"id": "555", "name": "Ada", "properties": [{"id": GA_PROPERTY_ID, "name": "Ada's Bakery"}]}]
    streams = client.get(f"{GOOGLE}/properties/{GA_PROPERTY_ID}/streams").json()["data"]
    assert streams == [
        {"id": GA_STREAM_ID, "name": "Bakery site", "measurementId": GA_MEASUREMENT_ID, "url": "https://ada.example"}
    ]

    status = client.get(SITE).json()["data"]
    assert status["link"] is None and status["connection"]["connected"] is True
    assert status["createUrl"].startswith("https://analytics.google.com/")

    link = client.put(SITE, json=LINK).json()["data"]
    assert link["measurementId"] == GA_MEASUREMENT_ID and link["propertyName"] == "Ada's Bakery"
    assert link["timeZone"] == "Europe/London" and link["tagPublished"] is False
    assert client.get(SITE).json()["data"]["link"] == link

    report = client.get(f"{SITE}/report", params={"range": "7d"})
    assert report.status_code == 200, report.json()
    data = report.json()["data"]
    assert data["range"] == "7d" and len(data["daily"]) == 7 and data["activeNow"] == 3
    assert data["totals"]["users"] == 0 and data["topPages"] == []
    [(property_id, requests)] = google.report_calls
    assert property_id == GA_PROPERTY_ID and len(requests) == 7

    # Read again within a few minutes: no Google request.
    refreshes = google.refreshes
    assert client.get(f"{SITE}/report", params={"range": "7d"}).json()["data"] == data
    assert (len(google.report_calls), len(google.realtime_calls), google.refreshes) == (1, 1, refreshes)
    # Another range is another report, but the visitors right now are the same.
    assert client.get(f"{SITE}/report").json()["data"]["range"] == "28d"
    assert (len(google.report_calls), len(google.realtime_calls)) == (2, 1)


def test_publishing_a_linked_site_adds_its_tag_and_unlinking_takes_it_off(
    client: TestClient, github: FakeGithubClient
) -> None:
    base = linked_site(client)

    published = client.post(
        f"/api/v1/publish/{PROJECT_ID}",
        json={"target": "vercel", "baseCommit": base, "credentials": {"token": VALID_PLATFORM_TOKEN}},
    )
    assert published.status_code == 200, published.json()
    workflow = github.files[REPO][WORKFLOW_PATH].content
    assert f'GA_MEASUREMENT_ID: "{GA_MEASUREMENT_ID}"' in workflow and "Add the Google Analytics tag" in workflow
    assert client.get(SITE).json()["data"]["link"]["tagPublished"] is True

    assert client.delete(SITE).status_code == 204
    assert client.get(SITE).json()["data"]["link"] is None
    again = client.post(
        f"/api/v1/publish/{PROJECT_ID}", json={"target": "vercel", "baseCommit": published.json()["data"]["commitSha"]}
    )
    assert again.status_code == 200 and again.json()["data"]["changed"] is True
    assert "GA_MEASUREMENT_ID" not in github.files[REPO][WORKFLOW_PATH].content


def test_the_callback_redirects_with_the_error(client: TestClient, google: FakeGoogleClient) -> None:
    sign_in(client)
    assert callback(client, code=GOOGLE_CODE, state="x") == f"{APP_URL}/?analyticsError=invalid_state"

    query = start_connect(client, "/analytics?tab=1")
    assert (
        callback(client, code=GOOGLE_CODE, state="forged") == f"{APP_URL}/analytics?tab=1&analyticsError=invalid_state"
    )

    query = start_connect(client)
    denied = callback(client, error="access_denied", state=query["state"][0])
    assert denied == f"{APP_URL}/analytics?analyticsError=google_denied"

    google.scope = "openid email"
    query = start_connect(client)
    unticked = callback(client, code=GOOGLE_CODE, state=query["state"][0])
    assert unticked == f"{APP_URL}/analytics?analyticsError=google_scope_missing"

    google.scope = "openid email https://www.googleapis.com/auth/analytics.readonly"
    query = start_connect(client)
    assert callback(client, code="wrong", state=query["state"][0]).endswith("analyticsError=google_code_invalid")
    assert client.get(GOOGLE).json()["data"]["connected"] is False


def test_an_unsafe_return_path_comes_back_to_the_start(client: TestClient) -> None:
    sign_in(client)
    query = start_connect(client, "//evil.example")
    assert callback(client, code=GOOGLE_CODE, state=query["state"][0]) == f"{APP_URL}/?analytics=connected"


def test_without_a_google_client_analytics_is_off(client: TestClient) -> None:
    client.app.dependency_overrides[get_google_client] = lambda: None  # type: ignore[attr-defined]
    sign_in(client)
    create_site(client)

    assert client.get(GOOGLE).json()["data"] == {"configured": False, "connected": False, "email": None}
    assert error(client.post(f"{GOOGLE}/connect", json={})) == (503, "analytics_not_configured")
    assert client.get(SITE).json()["data"]["connection"]["configured"] is False


def test_disconnecting_revokes_google_s_access_and_keeps_the_site_s_stream(
    client: TestClient, google: FakeGoogleClient
) -> None:
    linked_site(client)
    assert client.get(f"{SITE}/report").status_code == 200

    assert client.delete(GOOGLE).status_code == 204
    assert google.revoked == [GOOGLE_REFRESH_TOKEN]
    assert client.get(GOOGLE).json()["data"]["connected"] is False
    # Not even the report read a moment ago.
    assert error(client.get(f"{SITE}/report")) == (409, "google_not_connected")
    assert client.get(SITE).json()["data"]["link"]["streamId"] == GA_STREAM_ID
    assert client.delete(GOOGLE).status_code == 204


def test_a_token_google_refuses_is_forgotten_and_the_user_asked_to_reconnect(
    client: TestClient, google: FakeGoogleClient
) -> None:
    sign_in(client)
    connect(client)
    google.refresh_tokens.clear()

    assert error(client.get(f"{GOOGLE}/properties")) == (409, "google_reconnect")
    assert client.get(GOOGLE).json()["data"]["connected"] is False
    assert error(client.get(f"{GOOGLE}/properties")) == (409, "google_not_connected")


def test_a_stream_the_account_can_t_read_isn_t_linked(client: TestClient, google: FakeGoogleClient) -> None:
    sign_in(client)
    create_site(client)
    connect(client)

    assert error(client.put(SITE, json={**LINK, "streamId": "1"})) == (404, "analytics_not_found")
    google.denied.add(GA_PROPERTY_ID)
    assert error(client.put(SITE, json=LINK)) == (403, "analytics_access_denied")
    assert client.get(SITE).json()["data"]["link"] is None


def test_a_site_without_a_stream_has_no_report(client: TestClient) -> None:
    sign_in(client)
    create_site(client)
    connect(client)

    assert error(client.get(f"{SITE}/report")) == (404, "analytics_not_linked")
    assert client.get(f"{SITE}/report", params={"range": "1y"}).status_code == 422
    assert client.get(f"{GOOGLE}/properties/abc/streams").status_code == 422
    assert client.put(SITE, json={"propertyId": "properties/1", "streamId": "2"}).status_code == 422


def test_an_unknown_site_is_not_found(client: TestClient) -> None:
    sign_in(client)
    connect(client)

    assert error(client.get(SITE)) == (404, "site_not_found")
    assert error(client.put(SITE, json=LINK)) == (404, "site_not_found")


def test_start_over_forgets_the_site_s_stream(client: TestClient) -> None:
    linked_site(client)

    assert client.delete(f"/api/v1/ai/threads/{PROJECT_ID}").status_code == 204
    create_site(client)
    assert client.get(SITE).json()["data"]["link"] is None
