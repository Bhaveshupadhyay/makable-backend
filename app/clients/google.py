"""Google's OAuth endpoints and the Google Analytics Admin and Data APIs, for users who connect their Analytics. Only
the fixed hosts in `constants/analytics.py` are called. Tokens are only used for these requests and never logged."""

import logging
from typing import Any, Protocol
from urllib.parse import urlencode

import httpx
from fastapi import status

from app.constants.analytics import (
    ANALYTICS_ADMIN_API,
    ANALYTICS_DATA_API,
    GOOGLE_AUTHORIZE_URL,
    GOOGLE_REVOKE_URL,
    GOOGLE_SCOPES,
    GOOGLE_TIMEOUT_SECONDS,
    GOOGLE_TOKEN_URL,
    GOOGLE_USERINFO_URL,
    MAX_ACCOUNT_PAGES,
    MEASUREMENT_ID,
)
from app.core.client import HTTP_POOL_TIMEOUT_SECONDS
from app.core.exceptions import AppError, ExternalServiceError, NotFoundError
from app.core.limits import ServiceBusyError
from app.schemas.analytics import (
    GaAccountRead,
    GaProperty,
    GaPropertyRead,
    GaReport,
    GaRow,
    GaStream,
    GoogleTokens,
    GoogleUser,
)

logger = logging.getLogger(__name__)


class GoogleError(ExternalServiceError):
    code = "google_error"
    message = "Google didn't answer as expected. Try again shortly."


class GoogleTokenRefusedError(Exception):
    """Google's token endpoint refused a code or refresh token (`invalid_grant`): revoked, expired or already used."""


class AnalyticsAccessDeniedError(AppError):
    status_code = status.HTTP_403_FORBIDDEN
    code = "analytics_access_denied"
    message = "Your Google account can't read that Analytics property. Check its access in Google Analytics."


class AnalyticsNotFoundError(NotFoundError):
    code = "analytics_not_found"
    message = "That Analytics property or stream wasn't found in your Google account."


class GoogleRateLimitedError(AppError):
    """Google Analytics' quota for the property is used up for now. `details.retryAfter` is a wait in seconds."""

    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    code = "google_rate_limited"
    message = "Google Analytics is limiting requests for this property right now. Try again in a few minutes."

    def __init__(self, retry_after: int) -> None:
        super().__init__(details={"retryAfter": retry_after}, headers={"Retry-After": str(retry_after)})


class GoogleClient(Protocol):
    def authorize_url(self, *, redirect_uri: str, state: str, code_challenge: str) -> str: ...

    async def exchange_code(self, *, code: str, code_verifier: str, redirect_uri: str) -> GoogleTokens: ...

    async def refresh(self, refresh_token: str) -> GoogleTokens: ...

    async def revoke(self, token: str) -> None: ...

    async def get_user(self, access_token: str) -> GoogleUser: ...

    async def list_accounts(self, access_token: str) -> list[GaAccountRead]: ...

    async def get_property(self, access_token: str, property_id: str) -> GaProperty: ...

    async def list_streams(self, access_token: str, property_id: str) -> list[GaStream]: ...

    async def get_stream(self, access_token: str, property_id: str, stream_id: str) -> GaStream: ...

    async def run_reports(
        self, access_token: str, property_id: str, requests: list[dict[str, Any]]
    ) -> list[GaReport]: ...

    async def run_realtime_report(self, access_token: str, property_id: str, request: dict[str, Any]) -> GaReport: ...


class HttpGoogleClient:
    """Talks to Google over the shared `httpx.AsyncClient`, as makable's OAuth client."""

    # `batchRunReports` takes at most this many reports.
    MAX_BATCH = 5

    def __init__(self, http: httpx.AsyncClient, *, client_id: str, client_secret: str) -> None:
        self._http = http
        self._client_id = client_id
        self._client_secret = client_secret

    def authorize_url(self, *, redirect_uri: str, state: str, code_challenge: str) -> str:
        """Google's consent page. `access_type=offline` with `prompt=consent` always returns a refresh token, also
        to a user who connected before."""
        query = {
            "client_id": self._client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": GOOGLE_SCOPES,
            "access_type": "offline",
            "prompt": "consent",
            "include_granted_scopes": "true",
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
        return f"{GOOGLE_AUTHORIZE_URL}?{urlencode(query)}"

    async def exchange_code(self, *, code: str, code_verifier: str, redirect_uri: str) -> GoogleTokens:
        """Redeems the code from the consent page.

        Raises:
            GoogleTokenRefusedError: The code is wrong, expired or used, or the verifier doesn't match.
            GoogleError: Google failed.
        """
        return await self._token(
            {
                "grant_type": "authorization_code",
                "code": code,
                "code_verifier": code_verifier,
                "redirect_uri": redirect_uri,
            }
        )

    async def refresh(self, refresh_token: str) -> GoogleTokens:
        """A new access token (an hour) for a stored refresh token.

        Raises:
            GoogleTokenRefusedError: The user revoked makable's access, or the token expired.
            GoogleError: Google failed.
        """
        return await self._token({"grant_type": "refresh_token", "refresh_token": refresh_token})

    async def revoke(self, token: str) -> None:
        """Ends makable's access to the user's Google account. A token Google doesn't know any more is fine.

        Raises:
            GoogleError: Google couldn't be reached or failed.
        """
        res = await self._send("POST", GOOGLE_REVOKE_URL, data={"token": token})
        if res.is_error and res.status_code != status.HTTP_400_BAD_REQUEST:
            raise GoogleError()

    async def get_user(self, access_token: str) -> GoogleUser:
        """The Google account the token belongs to.

        Raises:
            GoogleError: Google failed.
        """
        data = _object(await self._api("GET", GOOGLE_USERINFO_URL, access_token))
        match data:
            case {"sub": str(sub)}:
                email = data.get("email")
                return GoogleUser(sub=sub, email=email if isinstance(email, str) else None)
        raise GoogleError()

    async def list_accounts(self, access_token: str) -> list[GaAccountRead]:
        """The user's Analytics accounts and their GA4 properties.

        Raises:
            AnalyticsAccessDeniedError / GoogleRateLimitedError / GoogleError: Google refused or failed.
        """
        accounts: list[GaAccountRead] = []
        page_token: str | None = None
        for _ in range(MAX_ACCOUNT_PAGES):
            params = {"pageSize": "200", **({"pageToken": page_token} if page_token else {})}
            data = _object(
                await self._api("GET", f"{ANALYTICS_ADMIN_API}/accountSummaries", access_token, params=params)
            )
            for summary in _list(data.get("accountSummaries")):
                account = _account(summary)
                if account is not None:
                    accounts.append(account)
            page_token = data.get("nextPageToken") or None
            if not isinstance(page_token, str):
                break
        return accounts

    async def get_property(self, access_token: str, property_id: str) -> GaProperty:
        """A property's name and time zone.

        Raises:
            AnalyticsNotFoundError: No such property.
            AnalyticsAccessDeniedError: The user can't read it.
            GoogleRateLimitedError / GoogleError: Google limited or failed.
        """
        data = _object(await self._api("GET", f"{ANALYTICS_ADMIN_API}/properties/{property_id}", access_token))
        match data:
            case {"displayName": str(name)}:
                time_zone = data.get("timeZone")
                return GaProperty(
                    id=property_id, name=name, time_zone=time_zone if isinstance(time_zone, str) else "Etc/UTC"
                )
        raise GoogleError()

    async def list_streams(self, access_token: str, property_id: str) -> list[GaStream]:
        """The property's web streams (app streams are left out).

        Raises:
            The errors of `get_property`.
        """
        url = f"{ANALYTICS_ADMIN_API}/properties/{property_id}/dataStreams"
        data = _object(await self._api("GET", url, access_token, params={"pageSize": "200"}))
        return [stream for entry in _list(data.get("dataStreams")) if (stream := _stream(entry)) is not None]

    async def get_stream(self, access_token: str, property_id: str, stream_id: str) -> GaStream:
        """One web stream of the property.

        Raises:
            AnalyticsNotFoundError: No such stream, or it isn't a web stream.
            The other errors of `get_property`.
        """
        url = f"{ANALYTICS_ADMIN_API}/properties/{property_id}/dataStreams/{stream_id}"
        stream = _stream(await self._api("GET", url, access_token))
        if stream is None:
            raise AnalyticsNotFoundError()
        return stream

    async def run_reports(self, access_token: str, property_id: str, requests: list[dict[str, Any]]) -> list[GaReport]:
        """Runs reports on the property, `MAX_BATCH` per request, and returns them in order.

        Raises:
            The errors of `get_property`.
        """
        reports: list[GaReport] = []
        for start in range(0, len(requests), self.MAX_BATCH):
            body = {"requests": requests[start : start + self.MAX_BATCH]}
            url = f"{ANALYTICS_DATA_API}/properties/{property_id}:batchRunReports"
            data = _object(await self._api("POST", url, access_token, json=body))
            batch = [_report(r) for r in _list(data.get("reports"))]
            if len(batch) != len(body["requests"]):
                raise GoogleError()
            reports += batch
        return reports

    async def run_realtime_report(self, access_token: str, property_id: str, request: dict[str, Any]) -> GaReport:
        """A report on the last 30 minutes.

        Raises:
            The errors of `get_property`.
        """
        url = f"{ANALYTICS_DATA_API}/properties/{property_id}:runRealtimeReport"
        return _report(await self._api("POST", url, access_token, json=request))

    async def _token(self, form: dict[str, str]) -> GoogleTokens:
        form = {**form, "client_id": self._client_id, "client_secret": self._client_secret}
        res = await self._send("POST", GOOGLE_TOKEN_URL, data=form)
        if res.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_401_UNAUTHORIZED):
            error = _object_or_empty(res).get("error")
            if error == "invalid_grant":
                raise GoogleTokenRefusedError()
            # `invalid_client` and the like: makable's OAuth client is set up wrong.
            logger.error("Google refused makable's OAuth client", extra={"status": res.status_code, "error": error})
            raise GoogleError()
        if res.is_error:
            logger.warning("Google token endpoint failed", extra={"status": res.status_code})
            raise GoogleError()
        try:
            return GoogleTokens.model_validate(res.json())
        except ValueError as err:
            raise GoogleError() from err

    async def _api(
        self,
        method: str,
        url: str,
        access_token: str,
        *,
        params: dict[str, str] | None = None,
        json: dict[str, Any] | None = None,
    ) -> Any:
        res = await self._send(
            method, url, params=params, json=json, headers={"Authorization": f"Bearer {access_token}"}
        )
        if res.is_error:
            _raise_for_api_status(res)
        try:
            return res.json()
        except ValueError as err:
            raise GoogleError() from err

    async def _send(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, str] | None = None,
        json: dict[str, Any] | None = None,
        data: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        try:
            return await self._http.request(
                method,
                url,
                params=params,
                json=json,
                data=data,
                headers={"Accept": "application/json", **(headers or {})},
                timeout=httpx.Timeout(GOOGLE_TIMEOUT_SECONDS, pool=HTTP_POOL_TIMEOUT_SECONDS),
            )
        except httpx.PoolTimeout as err:
            raise ServiceBusyError() from err
        except httpx.HTTPError as err:
            logger.warning("Google unreachable", extra={"error": repr(err)})
            raise GoogleError("Google couldn't be reached. Try again shortly.") from err


def _raise_for_api_status(res: httpx.Response) -> None:
    """Turns an Admin or Data API error into a domain error. Google's message is logged, never shown."""
    error = _object_or_empty(res).get("error")
    message = error.get("message", "") if isinstance(error, dict) else ""
    extra = {"status": res.status_code, "error": str(message)[:500]}
    if res.status_code == status.HTTP_429_TOO_MANY_REQUESTS:
        raise GoogleRateLimitedError(_retry_after(res))
    if res.status_code == status.HTTP_403_FORBIDDEN:
        if "SERVICE_DISABLED" in res.text or "has not been used in project" in res.text:
            # makable's Google Cloud project doesn't have the API turned on: nothing the user can do.
            logger.error("A Google Analytics API is off for makable's OAuth client", extra=extra)
            raise GoogleError()
        logger.info("Google Analytics refused access", extra=extra)
        raise AnalyticsAccessDeniedError()
    if res.status_code == status.HTTP_404_NOT_FOUND:
        raise AnalyticsNotFoundError()
    logger.warning("Google Analytics failed", extra=extra)
    raise GoogleError()


def _retry_after(res: httpx.Response) -> int:
    try:
        return max(1, min(int(float(res.headers.get("retry-after", ""))), 3600))
    except ValueError:
        # GA's quotas are counted per hour.
        return 300


def _object_or_empty(res: httpx.Response) -> dict[str, Any]:
    try:
        data = res.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _object(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise GoogleError()
    return data


def _list(data: Any) -> list[Any]:
    return data if isinstance(data, list) else []


def _account(summary: Any) -> GaAccountRead | None:
    match summary:
        case {"account": str(account)}:
            pass
        case _:
            return None
    properties = []
    for entry in _list(summary.get("propertySummaries")):
        match entry:
            # Only ordinary GA4 properties: rollup and subproperties can't take a site's tag.
            case {"property": str(name), "displayName": str(display)} if entry.get("propertyType") in (
                None,
                "PROPERTY_TYPE_ORDINARY",
            ):
                properties.append(GaPropertyRead(id=name.removeprefix("properties/"), name=display))
    display_name = summary.get("displayName")
    return GaAccountRead(
        id=account.removeprefix("accounts/"),
        name=display_name if isinstance(display_name, str) else account,
        properties=properties,
    )


def _stream(entry: Any) -> GaStream | None:
    match entry:
        case {"name": str(name), "type": "WEB_DATA_STREAM", "webStreamData": {"measurementId": str(measurement_id)}}:
            pass
        case _:
            return None
    if not MEASUREMENT_ID.fullmatch(measurement_id):
        return None
    display = entry.get("displayName")
    url = entry["webStreamData"].get("defaultUri")
    return GaStream(
        id=name.rsplit("/", 1)[-1],
        name=display if isinstance(display, str) and display else measurement_id,
        measurement_id=measurement_id,
        url=url if isinstance(url, str) and url else None,
    )


def _report(data: Any) -> GaReport:
    """A Data API report's rows. A report with no data has no `rows`."""
    rows = []
    for row in _list(_object(data).get("rows")):
        if not isinstance(row, dict):
            continue
        rows.append(
            GaRow(
                dimensions=[_value(v) for v in _list(row.get("dimensionValues"))],
                metrics=[_value(v) for v in _list(row.get("metricValues"))],
            )
        )
    return GaReport(rows=rows)


def _value(entry: Any) -> str:
    value = entry.get("value") if isinstance(entry, dict) else None
    return value if isinstance(value, str) else ""
