"""The Google Analytics connection and site links on the test database and the fake Google: what's stored, and
what happens when Google or the stored token fails."""

from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.google import GoogleError
from app.constants.analytics import ReportRange
from app.core.security import PayloadSigner
from app.models.google_credential import GoogleCredential
from app.repositories.google_credential import SqlGoogleCredentialRepository
from app.repositories.site import SqlSiteRepository
from app.repositories.site_analytics import SqlSiteAnalyticsRepository
from app.repositories.unit_of_work import SqlAlchemyUnitOfWork
from app.schemas.analytics import GaReport, GaRow, SiteAnalyticsLink
from app.services.analytics_reports import TtlCache
from app.services.analytics_service import AnalyticsService, GoogleReconnectError
from app.services.site_service import SiteNotFoundError
from app.tests.ai_edit_fixtures import PROJECT_ID, USER
from app.tests.fakes import (
    FAKE_CIPHER,
    GA_PROPERTY_ID,
    GA_STREAM_ID,
    GOOGLE_CODE,
    GOOGLE_REFRESH_TOKEN,
    FakeGoogleClient,
)

LINK = SiteAnalyticsLink(property_id=GA_PROPERTY_ID, stream_id=GA_STREAM_ID)


def service(session: AsyncSession, google: FakeGoogleClient | None) -> AnalyticsService:
    return AnalyticsService(
        google,
        SqlGoogleCredentialRepository(session),
        SqlSiteAnalyticsRepository(session),
        SqlSiteRepository(session),
        FAKE_CIPHER,
        PayloadSigner("s" * 32, salt="google-oauth-state"),
        SqlAlchemyUnitOfWork(session),
        callback_url="http://localhost:5173/api/v1/analytics/google/callback",
        reports=TtlCache(600),
        live=TtlCache(60),
    )


async def connect(analytics: AnalyticsService, google: FakeGoogleClient) -> None:
    start = analytics.start_connect(USER, "/analytics")
    state = start.authorize_url.split("state=")[1].split("&")[0]
    await analytics.complete_connect(state_cookie=start.state_cookie, code=GOOGLE_CODE, state=state, error=None)


@pytest.fixture
def google() -> FakeGoogleClient:
    return FakeGoogleClient()


async def test_the_refresh_token_is_stored_encrypted(
    session: AsyncSession, google: FakeGoogleClient, site: str
) -> None:
    analytics = service(session, google)
    await connect(analytics, google)

    stored = await session.get(GoogleCredential, USER.id)
    assert stored is not None and stored.email == "ada@example.com"
    assert GOOGLE_REFRESH_TOKEN not in stored.refresh_token
    assert FAKE_CIPHER.decrypt(stored.refresh_token) == GOOGLE_REFRESH_TOKEN


async def test_another_user_s_site_is_not_found(session: AsyncSession, google: FakeGoogleClient, site: str) -> None:
    analytics = service(session, google)
    other = USER.model_copy(update={"id": uuid4(), "login": "mallory"})

    for call in (
        analytics.site_status(other, PROJECT_ID),
        analytics.link_site(other, PROJECT_ID, LINK),
        analytics.unlink_site(other, PROJECT_ID),
        analytics.report(other, PROJECT_ID, ReportRange.LAST_7_DAYS),
    ):
        with pytest.raises(SiteNotFoundError):
            await call


async def test_disconnect_forgets_the_token_even_when_google_can_t_be_reached(
    session: AsyncSession, google: FakeGoogleClient, site: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    analytics = service(session, google)
    await connect(analytics, google)

    async def unreachable(_: str) -> None:
        raise GoogleError()

    monkeypatch.setattr(google, "revoke", unreachable)
    await analytics.disconnect(USER)

    assert await session.get(GoogleCredential, USER.id) is None


async def test_a_token_that_can_t_be_decrypted_any_more_asks_to_reconnect(
    session: AsyncSession, google: FakeGoogleClient, site: str
) -> None:
    analytics = service(session, google)
    await connect(analytics, google)
    stored = await session.get(GoogleCredential, USER.id)
    assert stored is not None
    stored.refresh_token = "not-a-fernet-token"
    await session.commit()

    with pytest.raises(GoogleReconnectError):
        await analytics.accounts(USER)
    assert google.refreshes == 0
    assert await session.get(GoogleCredential, USER.id) is None


async def test_the_report_is_read_from_the_linked_stream(
    session: AsyncSession, google: FakeGoogleClient, site: str
) -> None:
    analytics = service(session, google)
    await connect(analytics, google)
    await analytics.link_site(USER, PROJECT_ID, LINK)
    totals = GaRow(dimensions=[], metrics=["12", "10", "15", "40", "30", "0.5", "0.5"])
    google.reports = [GaReport(rows=[totals]), *(GaReport(rows=[]) for _ in range(6))]

    report = await analytics.report(USER, PROJECT_ID, ReportRange.LAST_28_DAYS)

    assert report.totals.users == 12 and report.totals.page_views == 40 and report.active_now == 3
    [(_, requests)] = google.report_calls
    assert {r["dimensionFilter"]["filter"]["stringFilter"]["value"] for r in requests} == {GA_STREAM_ID}
    [(_, realtime)] = google.realtime_calls
    assert realtime["dimensionFilter"]["filter"]["stringFilter"]["value"] == GA_STREAM_ID
