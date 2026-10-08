"""The real Supabase client against a mocked HTTP transport, with tokens signed by a throwaway ES256 key."""

import json
from collections.abc import Iterator
from datetime import timedelta
from typing import Any
from uuid import uuid4

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec

from app.clients import supabase as supabase_module
from app.clients.supabase import HttpSupabaseAuthClient, SupabaseError, SupabaseRejectedError
from app.core.database import utc_now
from app.core.security import InvalidTokenError

URL = "https://project.supabase.co"
ISSUER = f"{URL}/auth/v1"
KEY = ec.generate_private_key(ec.SECP256R1())
KID = "key-1"


def jwks() -> dict[str, Any]:
    public = json.loads(jwt.algorithms.ECAlgorithm.to_jwk(KEY.public_key()))
    return {"keys": [{**public, "kid": KID, "alg": "ES256", "use": "sig"}]}


def sign(*, kid: str = KID, **overrides: Any) -> str:
    claims = {"sub": str(uuid4()), "aud": "authenticated", "iss": ISSUER, "exp": utc_now() + timedelta(hours=1)}
    return jwt.encode({**claims, **overrides}, KEY, algorithm="ES256", headers={"kid": kid})


class Recorder:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.responses: dict[str, httpx.Response] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/auth/v1/.well-known/jwks.json":
            return httpx.Response(200, json=jwks())
        return self.responses.get(request.url.path, httpx.Response(404))


@pytest.fixture(autouse=True)
def clear_jwks_cache() -> Iterator[None]:
    supabase_module._jwks_cache.clear()
    yield
    supabase_module._jwks_cache.clear()


@pytest.fixture
def recorder() -> Recorder:
    return Recorder()


@pytest.fixture
async def client(recorder: Recorder) -> Any:
    async with httpx.AsyncClient(transport=httpx.MockTransport(recorder)) as http:
        yield HttpSupabaseAuthClient(http, url=URL + "/", api_key="sb_publishable_test")


def test_authorize_url() -> None:
    client = HttpSupabaseAuthClient(httpx.AsyncClient(), url=URL, api_key="k")
    url = httpx.URL(
        client.authorize_url(provider="github", scopes="repo", redirect_to="http://x/cb", code_challenge="c")
    )

    assert str(url.copy_with(query=None)) == f"{ISSUER}/authorize"
    assert dict(url.params) == {
        "provider": "github",
        "scopes": "repo",
        "redirect_to": "http://x/cb",
        "code_challenge": "c",
        "code_challenge_method": "s256",
    }


async def test_verify_accepts_a_valid_token_and_caches_the_keys(
    client: HttpSupabaseAuthClient, recorder: Recorder
) -> None:
    sub = uuid4()

    claims = await client.verify(sign(sub=str(sub)))
    await client.verify(sign())

    assert claims.sub == sub
    assert len(recorder.requests) == 1
    assert recorder.requests[0].headers["apikey"] == "sb_publishable_test"


@pytest.mark.parametrize(
    "token",
    [
        pytest.param(lambda: sign(aud="anon"), id="wrong audience"),
        pytest.param(lambda: sign(iss="https://other.supabase.co/auth/v1"), id="wrong issuer"),
        pytest.param(lambda: sign(exp=utc_now() - timedelta(minutes=1)), id="expired"),
        pytest.param(lambda: sign(kid="unknown"), id="unknown key"),
        pytest.param(
            lambda: jwt.encode({"sub": str(uuid4())}, "x" * 32, algorithm="HS256", headers={"kid": KID}),
            id="HS256 with a known kid",
        ),
        pytest.param(lambda: "not.a.jwt", id="garbage"),
    ],
)
async def test_verify_rejects(client: HttpSupabaseAuthClient, token: Any) -> None:
    with pytest.raises(InvalidTokenError):
        await client.verify(token())


async def test_verify_with_no_published_keys_is_a_service_error() -> None:
    def no_keys(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"keys": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(no_keys)) as http:
        client = HttpSupabaseAuthClient(http, url=URL, api_key="k")
        with pytest.raises(SupabaseError):
            await client.verify(sign())


async def test_exchange_code(client: HttpSupabaseAuthClient, recorder: Recorder) -> None:
    user_id = uuid4()
    recorder.responses["/auth/v1/token"] = httpx.Response(
        200,
        json={
            "access_token": "at",
            "refresh_token": "rt",
            "expires_in": 3600,
            "token_type": "bearer",
            "user": {"id": str(user_id), "email": "o@example.com"},
            "provider_token": "gho_x",
        },
    )

    session = await client.exchange_code(auth_code="code", code_verifier="verifier")

    request = recorder.requests[0]
    assert request.url.params["grant_type"] == "pkce"
    assert json.loads(request.content) == {"auth_code": "code", "code_verifier": "verifier"}
    assert (session.user.id, session.provider_token) == (user_id, "gho_x")


async def test_4xx_is_a_rejection_and_5xx_a_service_error(client: HttpSupabaseAuthClient, recorder: Recorder) -> None:
    recorder.responses["/auth/v1/token"] = httpx.Response(400, json={"error_code": "refresh_token_not_found"})
    with pytest.raises(SupabaseRejectedError, match="refresh_token_not_found"):
        await client.refresh("rt")

    recorder.responses["/auth/v1/token"] = httpx.Response(503)
    with pytest.raises(SupabaseError) as exc:
        await client.refresh("rt")
    assert not isinstance(exc.value, SupabaseRejectedError)


async def test_sign_out_sends_the_access_token(client: HttpSupabaseAuthClient, recorder: Recorder) -> None:
    recorder.responses["/auth/v1/logout"] = httpx.Response(204)

    await client.sign_out("at")

    request = recorder.requests[0]
    assert request.headers["authorization"] == "Bearer at"
    assert request.url.params["scope"] == "local"
