from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient

from app.constants.auth import (
    ACCESS_TOKEN_COOKIE,
    ACCESS_TOKEN_COOKIE_PATH,
    OAUTH_STATE_COOKIE,
    OAUTH_STATE_COOKIE_PATH,
    REFRESH_TOKEN_COOKIE,
    REFRESH_TOKEN_COOKIE_PATH,
)
from app.tests.conftest import APP_URL
from app.tests.fakes import VALID_CODE, FakeGithubClient, FakeSupabaseClient

AUTH = "/api/v1/auth"


def start_login(client: TestClient, return_to: str = "/") -> dict[str, list[str]]:
    """Runs the login step and returns the query of the Supabase authorize URL."""
    res = client.get(f"{AUTH}/github/login", params={"returnTo": return_to})
    assert res.status_code == 302
    location = urlsplit(res.headers["location"])
    assert f"{location.scheme}://{location.netloc}{location.path}" == "https://project.supabase.co/auth/v1/authorize"
    return parse_qs(location.query)


def callback(client: TestClient, **params: str) -> str:
    res = client.get(f"{AUTH}/github/callback", params=params)
    assert res.status_code == 302
    return res.headers["location"]


def sign_in(client: TestClient) -> None:
    start_login(client)
    callback(client, code=VALID_CODE)


def refresh_cookie(client: TestClient) -> str | None:
    return client.cookies.get(REFRESH_TOKEN_COOKIE, path=REFRESH_TOKEN_COOKIE_PATH)


def test_session_is_401_with_an_error_envelope_when_signed_out(client: TestClient) -> None:
    res = client.get(f"{AUTH}/session")

    assert res.status_code == 401
    body = res.json()
    assert body["success"] is False
    assert body["error"]["code"] == "unauthorized"
    assert body["requestId"] == res.headers["x-request-id"]


def test_login_asks_supabase_for_github_with_repo_scope(client: TestClient) -> None:
    query = start_login(client)

    assert query["provider"] == ["github"]
    assert query["scopes"] == ["repo"]
    assert query["redirect_to"] == [f"{APP_URL}/api/v1/auth/github/callback"]


def test_login_sets_cookies_and_returns_to_the_page(client: TestClient, github: FakeGithubClient) -> None:
    start_login(client, "/projects?tab=code")

    assert callback(client, code=VALID_CODE) == f"{APP_URL}/projects?tab=code"
    assert client.cookies.get(ACCESS_TOKEN_COOKIE, path=ACCESS_TOKEN_COOKIE_PATH)
    assert refresh_cookie(client)
    assert client.cookies.get(OAUTH_STATE_COOKIE, path=OAUTH_STATE_COOKIE_PATH) is None
    # The profile comes from GitHub, fetched with the token Supabase handed over.
    assert github.tokens_seen == ["gho_access"]

    res = client.get(f"{AUTH}/session")
    assert res.status_code == 200
    body = res.json()
    assert body["success"] is True
    user = body["data"]["user"]
    assert user == {
        "id": user["id"],
        "login": "octocat",
        "name": "The Octocat",
        "avatarUrl": "https://github.com/octocat.png",
        "role": "user",
    }


def test_login_again_updates_the_same_user(client: TestClient, github: FakeGithubClient) -> None:
    sign_in(client)
    first = client.get(f"{AUTH}/session").json()["data"]["user"]

    github.user = github.user.model_copy(update={"login": "renamed"})
    sign_in(client)
    second = client.get(f"{AUTH}/session").json()["data"]["user"]

    assert second["id"] == first["id"]
    assert second["login"] == "renamed"


def test_failed_callbacks_redirect_with_auth_error(client: TestClient, supabase: FakeSupabaseClient) -> None:
    # Without the state cookie the return path isn't trusted.
    assert callback(client, code=VALID_CODE) == f"{APP_URL}/?authError=invalid_state"

    start_login(client, "/projects")
    assert callback(client, error="access_denied") == f"{APP_URL}/projects?authError=access_denied"

    start_login(client, "/projects")
    assert callback(client, code="expired") == f"{APP_URL}/projects?authError=auth_error"

    supabase.provider_token = None
    start_login(client, "/projects")
    assert callback(client, code=VALID_CODE) == f"{APP_URL}/projects?authError=github_error"

    assert client.get(f"{AUTH}/session").status_code == 401


def test_refresh_rotates_the_tokens(client: TestClient, supabase: FakeSupabaseClient) -> None:
    sign_in(client)
    old_refresh = refresh_cookie(client)
    supabase.expire_access_tokens()
    assert client.get(f"{AUTH}/session").status_code == 401

    assert client.post(f"{AUTH}/refresh").status_code == 204

    assert client.get(f"{AUTH}/session").status_code == 200
    assert refresh_cookie(client) != old_refresh
    # The old refresh token was used up.
    client.cookies.set(REFRESH_TOKEN_COOKIE, old_refresh or "", path=REFRESH_TOKEN_COOKIE_PATH)
    assert client.post(f"{AUTH}/refresh").status_code == 401


def test_refresh_without_a_session_is_401(client: TestClient) -> None:
    assert client.post(f"{AUTH}/refresh").status_code == 401


def test_logout_revokes_the_refresh_token(client: TestClient) -> None:
    sign_in(client)
    refresh_token = refresh_cookie(client)

    assert client.post(f"{AUTH}/logout").status_code == 204
    assert client.get(f"{AUTH}/session").status_code == 401

    # Revoked in Supabase, not just cleared from the browser.
    client.cookies.set(REFRESH_TOKEN_COOKIE, refresh_token or "", path=REFRESH_TOKEN_COOKIE_PATH)
    assert client.post(f"{AUTH}/refresh").status_code == 401


def test_logout_with_an_expired_access_token_still_revokes(client: TestClient, supabase: FakeSupabaseClient) -> None:
    sign_in(client)
    supabase.expire_access_tokens()

    assert client.post(f"{AUTH}/logout").status_code == 204

    assert len(supabase.signed_out) == 1
    assert refresh_cookie(client) is None


def test_logout_when_signed_out_is_a_no_op(client: TestClient) -> None:
    assert client.post(f"{AUTH}/logout").status_code == 204
