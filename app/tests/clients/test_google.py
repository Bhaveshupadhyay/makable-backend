import json
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from app.clients.google import (
    AnalyticsAccessDeniedError,
    AnalyticsNotFoundError,
    GoogleError,
    GoogleRateLimitedError,
    GoogleTokenRefusedError,
    HttpGoogleClient,
)
from app.constants.analytics import GOOGLE_SCOPES

REDIRECT = "http://localhost:5173/api/v1/analytics/google/callback"


def client(handler: Callable[[httpx.Request], httpx.Response]) -> HttpGoogleClient:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return HttpGoogleClient(http, client_id="client-id.apps.googleusercontent.com", client_secret="secret")


def form(request: httpx.Request) -> dict[str, list[str]]:
    return parse_qs(request.content.decode())


def web_stream(stream_id: str, measurement_id: str = "G-ABCD1234", **over: Any) -> dict[str, Any]:
    return {
        "name": f"properties/1/dataStreams/{stream_id}",
        "type": "WEB_DATA_STREAM",
        "displayName": "My site",
        "webStreamData": {"measurementId": measurement_id, "defaultUri": "https://my.site"},
        **over,
    }


def test_the_consent_page_asks_for_offline_read_only_analytics_with_pkce() -> None:
    url = urlsplit(
        client(lambda _: httpx.Response(200)).authorize_url(redirect_uri=REDIRECT, state="st", code_challenge="ch")
    )
    query = parse_qs(url.query)

    assert f"{url.scheme}://{url.netloc}{url.path}" == "https://accounts.google.com/o/oauth2/v2/auth"
    assert query["scope"] == [GOOGLE_SCOPES]
    assert "https://www.googleapis.com/auth/analytics.readonly" in GOOGLE_SCOPES.split()
    assert query["access_type"] == ["offline"] and query["prompt"] == ["consent"]
    assert query["code_challenge"] == ["ch"] and query["code_challenge_method"] == ["S256"]
    assert query["state"] == ["st"] and query["redirect_uri"] == [REDIRECT]
    assert query["client_id"] == ["client-id.apps.googleusercontent.com"]
    assert "client_secret" not in query


async def test_exchanges_the_code_with_the_verifier_and_the_client_secret() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200, json={"access_token": "ya29", "expires_in": 3599, "refresh_token": "1//r", "scope": "openid email"}
        )

    tokens = await client(handler).exchange_code(code="c", code_verifier="v", redirect_uri=REDIRECT)

    assert (tokens.access_token, tokens.refresh_token, tokens.scope) == ("ya29", "1//r", "openid email")
    [request] = seen
    assert str(request.url) == "https://oauth2.googleapis.com/token"
    sent = form(request)
    assert sent["grant_type"] == ["authorization_code"] and sent["code"] == ["c"] and sent["code_verifier"] == ["v"]
    assert sent["client_secret"] == ["secret"] and sent["redirect_uri"] == [REDIRECT]


async def test_a_refused_refresh_token_is_its_own_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert form(request)["grant_type"] == ["refresh_token"]
        return httpx.Response(400, json={"error": "invalid_grant", "error_description": "Token has been revoked."})

    with pytest.raises(GoogleTokenRefusedError):
        await client(handler).refresh("1//old")


async def test_a_misconfigured_oauth_client_is_a_google_error() -> None:
    with pytest.raises(GoogleError):
        await client(lambda _: httpx.Response(401, json={"error": "invalid_client"})).refresh("1//r")


async def test_revoking_a_token_google_no_longer_knows_is_fine() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(400, json={"error": "invalid_token"})

    await client(handler).revoke("1//r")
    assert str(seen[0].url) == "https://oauth2.googleapis.com/revoke" and form(seen[0])["token"] == ["1//r"]


async def test_lists_ordinary_properties_across_pages() -> None:
    pages = {
        None: {
            "accountSummaries": [
                {
                    "account": "accounts/1",
                    "displayName": "Ada",
                    "propertySummaries": [
                        {
                            "property": "properties/11",
                            "displayName": "Bakery",
                            "propertyType": "PROPERTY_TYPE_ORDINARY",
                        },
                        {"property": "properties/12", "displayName": "Rollup", "propertyType": "PROPERTY_TYPE_ROLLUP"},
                    ],
                }
            ],
            "nextPageToken": "p2",
        },
        "p2": {"accountSummaries": [{"account": "accounts/2", "displayName": "Empty"}]},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer ya29"
        assert request.url.path == "/v1beta/accountSummaries"
        return httpx.Response(200, json=pages[request.url.params.get("pageToken")])

    accounts = await client(handler).list_accounts("ya29")

    assert [(a.id, a.name, [(p.id, p.name) for p in a.properties]) for a in accounts] == [
        ("1", "Ada", [("11", "Bakery")]),
        ("2", "Empty", []),
    ]


async def test_lists_only_web_streams() -> None:
    body = {
        "dataStreams": [
            web_stream("21"),
            {"name": "properties/1/dataStreams/22", "type": "ANDROID_APP_DATA_STREAM", "androidAppStreamData": {}},
            web_stream("23", measurement_id="not-an-id"),
        ]
    }
    streams = await client(lambda _: httpx.Response(200, json=body)).list_streams("ya29", "1")

    assert [(s.id, s.name, s.measurement_id, s.url) for s in streams] == [
        ("21", "My site", "G-ABCD1234", "https://my.site")
    ]


async def test_an_app_stream_is_not_found() -> None:
    app_stream = {"name": "properties/1/dataStreams/22", "type": "IOS_APP_DATA_STREAM"}
    with pytest.raises(AnalyticsNotFoundError):
        await client(lambda _: httpx.Response(200, json=app_stream)).get_stream("ya29", "1", "22")


async def test_reads_a_property_with_its_time_zone() -> None:
    body = {"name": "properties/1", "displayName": "Bakery", "timeZone": "Asia/Kolkata"}
    prop = await client(lambda _: httpx.Response(200, json=body)).get_property("ya29", "1")
    assert (prop.id, prop.name, prop.time_zone) == ("1", "Bakery", "Asia/Kolkata")


async def test_runs_reports_five_at_a_time_and_keeps_their_order() -> None:
    batches: list[list[dict[str, Any]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://analyticsdata.googleapis.com/v1beta/properties/1:batchRunReports"
        requests = json.loads(request.content)["requests"]
        batches.append(requests)
        reports = [
            {"rows": [{"dimensionValues": [{"value": r["id"]}], "metricValues": [{"value": "7"}]}]} for r in requests
        ]
        return httpx.Response(200, json={"reports": reports})

    reports = await client(handler).run_reports("ya29", "1", [{"id": str(n)} for n in range(7)])

    assert [len(b) for b in batches] == [5, 2]
    assert [r.rows[0].dimensions[0] for r in reports] == [str(n) for n in range(7)]
    assert reports[0].rows[0].metrics == ["7"]


async def test_a_report_without_data_has_no_rows() -> None:
    report = await client(lambda _: httpx.Response(200, json={"rowCount": 0})).run_realtime_report("ya29", "1", {})
    assert report.rows == []


def google_error(code: int, status: str, message: str = "nope") -> httpx.Response:
    return httpx.Response(code, json={"error": {"code": code, "status": status, "message": message}})


@pytest.mark.parametrize(
    ("response", "error"),
    [
        (
            google_error(403, "PERMISSION_DENIED", "User does not have sufficient permissions"),
            AnalyticsAccessDeniedError,
        ),
        (
            google_error(403, "PERMISSION_DENIED", "Google Analytics Data API has not been used in project 1 before"),
            GoogleError,
        ),
        (google_error(404, "NOT_FOUND"), AnalyticsNotFoundError),
        (google_error(429, "RESOURCE_EXHAUSTED"), GoogleRateLimitedError),
        (google_error(500, "INTERNAL"), GoogleError),
        (httpx.Response(200, text="not json"), GoogleError),
    ],
)
async def test_api_failures(response: httpx.Response, error: type[Exception]) -> None:
    with pytest.raises(error):
        await client(lambda _: response).get_property("ya29", "1")


async def test_a_used_up_quota_says_how_long_to_wait() -> None:
    with pytest.raises(GoogleRateLimitedError) as caught:
        await client(lambda _: google_error(429, "RESOURCE_EXHAUSTED")).run_reports("ya29", "1", [{}])
    assert caught.value.details == {"retryAfter": 300}


async def test_an_unreachable_google_is_a_google_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(GoogleError):
        await client(handler).list_accounts("ya29")
