import asyncio
import logging
from collections.abc import Callable
from datetime import datetime
from uuid import UUID

from cryptography.fernet import InvalidToken
from pydantic import ValidationError

from app.clients.google import GoogleClient, GoogleError, GoogleTokenRefusedError
from app.constants.analytics import ANALYTICS_SCOPE, GA_CREATE_URL, ReportRange
from app.constants.auth import OAUTH_STATE_TTL_SECONDS
from app.core.database import utc_now
from app.core.exceptions import AppError, ConflictError, NotFoundError, ServiceUnavailableError
from app.core.limits import ServiceBusyError
from app.core.security import InvalidTokenError, PayloadSigner, TokenCipher, generate_token, pkce_challenge
from app.models.site_analytics import SiteAnalytics
from app.repositories.google_credential import GoogleCredentialRepository
from app.repositories.site import SiteRepository
from app.repositories.site_analytics import SiteAnalyticsRepository
from app.repositories.unit_of_work import UnitOfWork
from app.schemas.analytics import (
    AnalyticsReport,
    GaAccountRead,
    GaStreamRead,
    GoogleConnection,
    GoogleConnectStart,
    GoogleCredentialUpsert,
    GoogleState,
    SiteAnalyticsLink,
    SiteAnalyticsRead,
    SiteAnalyticsStatus,
    SiteAnalyticsUpsert,
)
from app.schemas.user import UserRead
from app.services.analytics_reports import (
    ANALYTICS_REPORTS,
    LIVE_VISITORS,
    ReportDates,
    TtlCache,
    build_report,
    live_visitors,
    realtime_request,
    report_requests,
)
from app.services.site_service import SiteNotFoundError
from app.utils.return_to import safe_return_to

logger = logging.getLogger(__name__)


class AnalyticsNotConfiguredError(ServiceUnavailableError):
    """makable has no Google OAuth client (`GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` unset)."""

    code = "analytics_not_configured"
    message = "Google Analytics isn't set up on this server."


class GoogleNotConnectedError(ConflictError):
    code = "google_not_connected"
    message = "Connect Google Analytics first."


class GoogleReconnectError(ConflictError):
    """Google refused the stored refresh token (access revoked, or it expired), so it was forgotten."""

    code = "google_reconnect"
    message = "makable can't read your Google Analytics any more. Connect Google again."


class AnalyticsNotLinkedError(NotFoundError):
    code = "analytics_not_linked"
    message = "This site isn't connected to a Google Analytics stream yet."


class GoogleConnectError(AppError):
    """The callback couldn't connect Google. It's turned into a redirect with `?analyticsError=<code>`, not JSON."""

    code = "google_connect_failed"
    message = "Google Analytics couldn't be connected."

    def __init__(self, code: str, message: str | None = None, *, return_to: str = "/") -> None:
        super().__init__(message, code=code)
        self.return_to = return_to


class AnalyticsService:
    """Google Analytics for users' sites. The user connects their Google account (read-only access to their
    Analytics, through makable's OAuth client), then picks the GA4 property and web stream each site reports to.
    Publishing a linked site adds the stream's tag to its pages (`PublishService`), and the site's report reads that
    stream's numbers from Google's Data API with the user's token.

    Only the refresh token is stored (encrypted); each request gets a fresh access token. Reports are kept a few
    minutes per worker, per user.
    """

    def __init__(
        self,
        google: GoogleClient | None,
        credentials: GoogleCredentialRepository,
        links: SiteAnalyticsRepository,
        sites: SiteRepository,
        cipher: TokenCipher,
        state_signer: PayloadSigner,
        uow: UnitOfWork,
        *,
        callback_url: str,
        reports: TtlCache[tuple[object, ...], AnalyticsReport] = ANALYTICS_REPORTS,
        live: TtlCache[tuple[object, ...], int] = LIVE_VISITORS,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._google = google
        self._credentials = credentials
        self._links = links
        self._sites = sites
        self._cipher = cipher
        self._state_signer = state_signer
        self._uow = uow
        self._callback_url = callback_url
        self._reports = reports
        self._live = live
        self._clock = clock

    async def connection(self, user: UserRead) -> GoogleConnection:
        """Whether the user's Google account is connected, and which one."""
        credential = await self._credentials.get(user.id)
        return GoogleConnection(
            configured=self._google is not None,
            connected=credential is not None,
            email=credential.email if credential else None,
        )

    def start_connect(self, user: UserRead, return_to: str | None) -> GoogleConnectStart:
        """Google's consent page, and the signed state the callback checks: the PKCE verifier, a random `state` and
        who's connecting (the callback is a navigation from Google, so the user is taken from the state).

        Args:
            user: The signed-in user.
            return_to: The SPA path to come back to. Unsafe values become `/`.

        Raises:
            AnalyticsNotConfiguredError: makable has no Google OAuth client.
        """
        google = self._client()
        oauth = GoogleState(
            user_id=user.id,
            code_verifier=generate_token(48),
            state=generate_token(),
            return_to=safe_return_to(return_to),
        )
        return GoogleConnectStart(
            authorize_url=google.authorize_url(
                redirect_uri=self._callback_url,
                state=oauth.state,
                code_challenge=pkce_challenge(oauth.code_verifier),
            ),
            state_cookie=self._state_signer.dumps(oauth.model_dump(mode="json")),
        )

    async def complete_connect(
        self, *, state_cookie: str | None, code: str | None, state: str | None, error: str | None
    ) -> str:
        """Handles the redirect back from Google: redeems the code and keeps the refresh token. Returns the SPA path
        to go back to.

        Raises:
            GoogleConnectError: The state cookie is missing, forged or expired, or doesn't match `state`; the user
                declined or didn't tick the Analytics permission; or Google failed. `return_to` is only trusted (and
                set) once the cookie has been verified.
        """
        oauth = self._verify_state(state_cookie)
        back = oauth.return_to
        if state != oauth.state:
            raise GoogleConnectError("invalid_state", "The Google sign-in didn't match this browser.", return_to=back)
        if error:
            code_ = "google_denied" if error == "access_denied" else "google_error"
            raise GoogleConnectError(code_, f"Google returned {error}", return_to=back)
        if not code:
            raise GoogleConnectError("missing_code", return_to=back)
        google = self._client()
        try:
            tokens = await google.exchange_code(
                code=code, code_verifier=oauth.code_verifier, redirect_uri=self._callback_url
            )
            if ANALYTICS_SCOPE not in tokens.scope.split():
                raise GoogleConnectError(
                    "google_scope_missing", "The Analytics permission wasn't granted.", return_to=back
                )
            if not tokens.refresh_token:
                raise GoogleConnectError("google_error", "Google returned no refresh token.", return_to=back)
            account = await google.get_user(tokens.access_token)
        except GoogleTokenRefusedError as err:
            raise GoogleConnectError("google_code_invalid", "Google refused the sign-in code.", return_to=back) from err
        except (GoogleError, ServiceBusyError) as err:
            raise GoogleConnectError(err.code, err.message, return_to=back) from err

        await self._credentials.upsert(
            GoogleCredentialUpsert(
                user_id=oauth.user_id,
                google_sub=account.sub,
                email=account.email,
                refresh_token=self._cipher.encrypt(tokens.refresh_token),
                scopes=tokens.scope,
            )
        )
        await self._uow.commit()
        logger.info("Google Analytics connected", extra={"user_id": str(oauth.user_id)})
        return back

    def return_path(self, state_cookie: str | None) -> str:
        """The SPA path the connect in `state_cookie` started from, or `/` when the cookie doesn't check out. For a
        callback that can't be handled now (a busy worker), so it still goes back to the page."""
        try:
            return self._verify_state(state_cookie).return_to
        except GoogleConnectError:
            return "/"

    async def disconnect(self, user: UserRead) -> None:
        """Ends makable's access to the user's Google account (revoked at Google when it can be) and forgets it. The
        sites keep their streams: their pages keep the tag, and reconnecting shows their reports again."""
        credential = await self._credentials.get(user.id)
        if credential is None:
            return
        try:
            refresh_token = self._cipher.decrypt(credential.refresh_token)
        except InvalidToken:
            refresh_token = None
        await self._credentials.delete(credential)
        await self._uow.commit()
        if refresh_token is not None and self._google is not None:
            try:
                await self._google.revoke(refresh_token)
            except (GoogleError, ServiceBusyError) as err:
                logger.warning("Couldn't revoke a Google token", extra={"error": err.message})
        logger.info("Google Analytics disconnected", extra={"user_id": str(user.id)})

    async def accounts(self, user: UserRead) -> list[GaAccountRead]:
        """The user's Analytics accounts and their GA4 properties, to pick from.

        Raises:
            AnalyticsNotConfiguredError: makable has no Google OAuth client.
            GoogleNotConnectedError: The user hasn't connected Google.
            GoogleReconnectError: Google refused the stored token.
            AnalyticsAccessDeniedError / GoogleRateLimitedError / GoogleError: Google refused or failed.
        """
        google = self._client()
        return await google.list_accounts(await self._access_token(user.id))

    async def streams(self, user: UserRead, property_id: str) -> list[GaStreamRead]:
        """The property's web streams, to pick from.

        Raises:
            AnalyticsNotFoundError: No such property in the user's account.
            The errors of `accounts`.
        """
        google = self._client()
        streams = await google.list_streams(await self._access_token(user.id), property_id)
        return [GaStreamRead.model_validate(s.model_dump()) for s in streams]

    async def site_status(self, user: UserRead, project_id: UUID) -> SiteAnalyticsStatus:
        """The user's Google connection and the stream the site reports to (no Google request).

        Raises:
            SiteNotFoundError: No such site, or it's another user's.
        """
        link = await self._site_link(user, project_id)
        return SiteAnalyticsStatus(
            connection=await self.connection(user),
            link=_read(link) if link else None,
            create_url=GA_CREATE_URL,
        )

    async def link_site(self, user: UserRead, project_id: UUID, data: SiteAnalyticsLink) -> SiteAnalyticsRead:
        """Makes the site report to a web stream of the user's, checked with Google. Its next publish adds the
        stream's tag to the site's pages.

        Raises:
            SiteNotFoundError: No such site, or it's another user's.
            AnalyticsNotFoundError: The property or stream isn't in the user's account (or isn't a web stream).
            The errors of `accounts`.
        """
        google = self._client()
        await self._site_link(user, project_id)
        token = await self._access_token(user.id)
        prop, stream = await asyncio.gather(
            google.get_property(token, data.property_id),
            google.get_stream(token, data.property_id, data.stream_id),
        )
        link = await self._links.upsert(
            SiteAnalyticsUpsert(
                site_id=project_id,
                property_id=prop.id,
                property_name=prop.name[:200],
                stream_id=stream.id,
                stream_name=stream.name[:200],
                measurement_id=stream.measurement_id,
                stream_url=stream.url[:500] if stream.url else None,
                time_zone=prop.time_zone[:64],
            )
        )
        result = _read(link)
        await self._uow.commit()
        logger.info("site analytics linked", extra={"user_id": str(user.id), "project_id": str(project_id)})
        return result

    async def unlink_site(self, user: UserRead, project_id: UUID) -> None:
        """Stops the site reporting to its stream. The tag leaves the site's pages with its next publish.

        Raises:
            SiteNotFoundError: No such site, or it's another user's.
        """
        await self._site_link(user, project_id)
        await self._links.delete_for_site(project_id)
        await self._uow.commit()

    async def report(self, user: UserRead, project_id: UUID, range_: ReportRange) -> AnalyticsReport:
        """The site's traffic over `range_`, read from Google (or from a report read in the last few minutes), with
        the users on the site right now.

        Raises:
            SiteNotFoundError: No such site, or it's another user's.
            AnalyticsNotLinkedError: The site has no stream.
            AnalyticsAccessDeniedError: The user's Google account can't read the property any more.
            The errors of `accounts`.
        """
        google = self._client()
        link = await self._site_link(user, project_id)
        if link is None:
            raise AnalyticsNotLinkedError()
        property_id, stream_id, time_zone = link.property_id, link.stream_id, link.time_zone
        # Checked before the caches too: a user who disconnected doesn't see the reports read before.
        if await self._credentials.get(user.id) is None:
            raise GoogleNotConnectedError()
        key = (user.id, property_id, stream_id)
        report = self._reports.get((*key, range_))
        active_now = self._live.get(key)
        if report is None or active_now is None:
            token = await self._access_token(user.id)
            if report is None:
                dates = ReportDates(range_, time_zone, self._clock())
                answers = await google.run_reports(token, property_id, report_requests(stream_id, dates))
                report = build_report(range_, dates, answers, fetched_at=self._clock())
                self._reports.put((*key, range_), report)
            if active_now is None:
                active_now = live_visitors(
                    await google.run_realtime_report(token, property_id, realtime_request(stream_id))
                )
                self._live.put(key, active_now)
        return report.model_copy(update={"active_now": active_now})

    def _client(self) -> GoogleClient:
        if self._google is None:
            raise AnalyticsNotConfiguredError()
        return self._google

    async def _access_token(self, user_id: UUID) -> str:
        """A fresh access token from the user's stored refresh token. Ends the request's transaction first: Google's
        answers can take a few seconds. A refresh token Google refuses is forgotten.

        Raises:
            GoogleNotConnectedError: The user hasn't connected Google.
            GoogleReconnectError: Google refused the stored token (or it can't be decrypted any more).
            GoogleError: Google failed.
        """
        credential = await self._credentials.get(user_id)
        if credential is None:
            raise GoogleNotConnectedError()
        try:
            refresh_token = self._cipher.decrypt(credential.refresh_token)
        except InvalidToken:
            refresh_token = None
        await self._uow.rollback()
        try:
            if refresh_token is None:
                raise GoogleTokenRefusedError()
            return (await self._client().refresh(refresh_token)).access_token
        except GoogleTokenRefusedError:
            await self._forget(user_id)
            raise GoogleReconnectError() from None

    async def _forget(self, user_id: UUID) -> None:
        credential = await self._credentials.get(user_id)
        if credential is not None:
            await self._credentials.delete(credential)
            await self._uow.commit()
        logger.info("Google refused a stored token; forgot it", extra={"user_id": str(user_id)})

    async def _site_link(self, user: UserRead, project_id: UUID) -> SiteAnalytics | None:
        site = await self._sites.get(project_id)
        if site is None or site.user_id != user.id:
            raise SiteNotFoundError()
        return await self._links.get(project_id)

    def _verify_state(self, state_cookie: str | None) -> GoogleState:
        if not state_cookie:
            raise GoogleConnectError("invalid_state", "Missing OAuth state")
        try:
            return GoogleState.model_validate(self._state_signer.loads(state_cookie, max_age=OAUTH_STATE_TTL_SECONDS))
        except (InvalidTokenError, ValidationError) as err:
            raise GoogleConnectError("invalid_state", "Invalid or expired OAuth state") from err


def _read(link: SiteAnalytics) -> SiteAnalyticsRead:
    return SiteAnalyticsRead(
        property_id=link.property_id,
        property_name=link.property_name,
        stream_id=link.stream_id,
        stream_name=link.stream_name,
        measurement_id=link.measurement_id,
        stream_url=link.stream_url,
        time_zone=link.time_zone,
        tag_published=link.published_measurement_id == link.measurement_id,
    )
