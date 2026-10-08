"""Supabase Auth client: OAuth sign-in with PKCE, session refresh and sign-out, and access token checks."""

import time
from typing import Any, Protocol
from urllib.parse import urlencode

import httpx
import jwt
from pydantic import ValidationError

from app.core.exceptions import ExternalServiceError
from app.core.security import InvalidTokenError
from app.schemas.auth import AccessTokenClaims
from app.schemas.supabase import SupabaseSession

# Supabase issues signed-in users' access tokens for this audience.
AUDIENCE = "authenticated"
# How long fetched signing keys are used before they're fetched again.
JWKS_TTL_SECONDS = 10 * 60
# A token signed with an unknown key triggers a refetch (keys rotate), but at most this often.
JWKS_MIN_REFETCH_SECONDS = 60

# Signing keys per JWKS URL, shared by every request: {url: (fetched_at, {kid: key})}.
_jwks_cache: dict[str, tuple[float, dict[str, jwt.PyJWK]]] = {}


class SupabaseError(ExternalServiceError):
    code = "supabase_error"
    message = "Supabase Auth request failed"


class SupabaseRejectedError(SupabaseError):
    """Supabase answered 4xx: the code, refresh token or access token isn't valid (any more)."""


class SupabaseAuthClient(Protocol):
    def authorize_url(self, *, provider: str, scopes: str, redirect_to: str, code_challenge: str) -> str: ...

    async def exchange_code(self, *, auth_code: str, code_verifier: str) -> SupabaseSession: ...

    async def refresh(self, refresh_token: str) -> SupabaseSession: ...

    async def sign_out(self, access_token: str) -> None: ...

    async def verify(self, access_token: str) -> AccessTokenClaims: ...


class HttpSupabaseAuthClient:
    """Talks to Supabase Auth over a shared `httpx.AsyncClient`.

    Access tokens are verified locally against the project's JWT signing keys (JWKS), which are cached,
    so an authenticated request costs no call to Supabase. That needs asymmetric signing keys, the
    default for new projects. A project still on the legacy shared secret publishes no keys.
    """

    def __init__(self, http: httpx.AsyncClient, *, url: str, api_key: str) -> None:
        self._http = http
        self._auth_url = f"{url.rstrip('/')}/auth/v1"
        self._api_key = api_key

    def authorize_url(self, *, provider: str, scopes: str, redirect_to: str, code_challenge: str) -> str:
        """The URL to send the browser to. Supabase sends it on to the provider, then back to `redirect_to`
        with `?code=` (or `?error=`). `redirect_to` must be in the project's allowed redirect URLs."""
        query = urlencode(
            {
                "provider": provider,
                "scopes": scopes,
                "redirect_to": redirect_to,
                "code_challenge": code_challenge,
                "code_challenge_method": "s256",
            }
        )
        return f"{self._auth_url}/authorize?{query}"

    async def exchange_code(self, *, auth_code: str, code_verifier: str) -> SupabaseSession:
        """Exchanges the code from the sign-in redirect for a session, including the provider's tokens.

        Raises:
            SupabaseRejectedError: The code is invalid or expired, or the verifier doesn't match.
            SupabaseError: Supabase couldn't be reached or answered unexpectedly.
        """
        return await self._token("pkce", {"auth_code": auth_code, "code_verifier": code_verifier})

    async def refresh(self, refresh_token: str) -> SupabaseSession:
        """Exchanges a refresh token for a new session. Supabase rotates it: the old one stops working.

        Raises:
            SupabaseRejectedError: The refresh token is unknown, used or revoked.
            SupabaseError: Supabase couldn't be reached or answered unexpectedly.
        """
        return await self._token("refresh_token", {"refresh_token": refresh_token})

    async def sign_out(self, access_token: str) -> None:
        """Ends the session the access token belongs to, revoking its refresh tokens. Access tokens
        already issued stay valid until they expire.

        Raises:
            SupabaseRejectedError: The access token is invalid or expired.
            SupabaseError: Supabase couldn't be reached or answered unexpectedly.
        """
        await self._post("/logout", params={"scope": "local"}, access_token=access_token)

    async def verify(self, access_token: str) -> AccessTokenClaims:
        """Verifies an access token's signature, audience, issuer and expiry.

        Raises:
            InvalidTokenError: The token is malformed, forged or expired.
            SupabaseError: The signing keys couldn't be fetched.
        """
        try:
            kid = jwt.get_unverified_header(access_token).get("kid")
        except jwt.PyJWTError as err:
            raise InvalidTokenError(str(err)) from err
        if not isinstance(kid, str):
            raise InvalidTokenError("token has no key id")
        key = await self._signing_key(kid)
        try:
            claims = jwt.decode(
                access_token,
                key,
                algorithms=[key.algorithm_name],
                audience=AUDIENCE,
                issuer=self._auth_url,
                options={"require": ["exp", "sub"]},
            )
            return AccessTokenClaims.model_validate(claims)
        except (jwt.PyJWTError, ValidationError) as err:
            raise InvalidTokenError(str(err)) from err

    async def _signing_key(self, kid: str) -> jwt.PyJWK:
        url = f"{self._auth_url}/.well-known/jwks.json"
        now = time.monotonic()
        fetched_at, keys = _jwks_cache.get(url, (0.0, {}))
        age = now - fetched_at
        if age > JWKS_TTL_SECONDS or (kid not in keys and age > JWKS_MIN_REFETCH_SECONDS):
            keys = await self._fetch_jwks(url)
            _jwks_cache[url] = (now, keys)
        if kid not in keys:
            raise InvalidTokenError("token is signed with an unknown key")
        return keys[kid]

    async def _fetch_jwks(self, url: str) -> dict[str, jwt.PyJWK]:
        try:
            res = await self._http.get(url, headers={"apikey": self._api_key})
            res.raise_for_status()
            key_set = jwt.PyJWKSet.from_dict(res.json())
        except (httpx.HTTPError, ValueError) as err:
            raise SupabaseError(f"fetching signing keys failed: {err}") from err
        except jwt.PyJWKSetError as err:
            raise SupabaseError(
                "Supabase published no usable signing keys. Is the project still on the legacy JWT secret?"
            ) from err
        return {key.key_id: key for key in key_set.keys if key.key_id}

    async def _token(self, grant_type: str, body: dict[str, str]) -> SupabaseSession:
        res = await self._post("/token", params={"grant_type": grant_type}, json=body)
        try:
            return SupabaseSession.model_validate(res.json())
        except ValueError as err:  # ValidationError is a ValueError.
            raise SupabaseError(f"unexpected {grant_type} token response") from err

    async def _post(
        self, path: str, *, params: dict[str, str], json: dict[str, str] | None = None, access_token: str | None = None
    ) -> httpx.Response:
        headers = {"apikey": self._api_key}
        if access_token:
            headers["Authorization"] = f"Bearer {access_token}"
        try:
            res = await self._http.post(f"{self._auth_url}{path}", params=params, json=json, headers=headers)
        except httpx.HTTPError as err:
            raise SupabaseError(f"{path} failed: {err}") from err
        if res.is_client_error:
            raise SupabaseRejectedError(f"{path} rejected: {_error_code(res)}")
        if res.is_error:
            raise SupabaseError(f"{path} failed: HTTP {res.status_code}")
        return res


def _error_code(res: httpx.Response) -> str:
    """Supabase's error code (e.g. `refresh_token_not_found`), for logs."""
    try:
        body: Any = res.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        return str(body.get("error_code") or body.get("error") or res.status_code)
    return str(res.status_code)
