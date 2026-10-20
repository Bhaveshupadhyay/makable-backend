"""Google Analytics for a user's sites: the Google connection, the GA4 property and web stream a site reports to, and
its traffic report. Google's tokens never go back to the browser."""

from datetime import date, datetime
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.constants.analytics import GA_ID, ReportRange
from app.schemas.common import CamelModel

GaId = Annotated[str, Field(pattern=f"^{GA_ID.pattern}$")]


class GoogleTokens(BaseModel):
    """Google's answer from its token endpoint."""

    access_token: str
    expires_in: int = 3600
    # Only when the user consents (`prompt=consent`); refreshing never returns a new one.
    refresh_token: str | None = None
    # The scopes the user granted, space-separated: with granular consent they can leave some unticked.
    scope: str = ""


class GoogleUser(BaseModel):
    sub: str
    email: str | None = None


class GoogleState(BaseModel):
    """What the connect step hands to the callback, in a signed cookie."""

    user_id: UUID
    code_verifier: str
    # Also sent as `state` and checked when Google sends it back.
    state: str
    return_to: str


class GoogleConnectStart(BaseModel):
    model_config = ConfigDict(frozen=True)

    authorize_url: str
    state_cookie: str


class GoogleCredentialUpsert(BaseModel):
    """What's stored for a user's Google connection. The refresh token is already encrypted."""

    user_id: UUID
    google_sub: str
    email: str | None
    refresh_token: str
    scopes: str


class GaProperty(BaseModel):
    """A GA4 property (Admin API `properties.get`)."""

    id: str
    name: str
    time_zone: str


class GaStream(BaseModel):
    """A web data stream (Admin API `dataStreams`)."""

    id: str
    name: str
    measurement_id: str
    url: str | None


class GaRow(BaseModel):
    dimensions: list[str]
    metrics: list[str]


class GaReport(BaseModel):
    """One report from the Data API: its rows, values in the order the request named them."""

    rows: list[GaRow]


class SiteAnalyticsUpsert(BaseModel):
    site_id: UUID
    property_id: str
    property_name: str
    stream_id: str
    stream_name: str
    measurement_id: str
    stream_url: str | None
    time_zone: str


# --- The API ---


class GoogleConnection(CamelModel):
    # makable has a Google OAuth client (`GOOGLE_CLIENT_ID`): analytics can be connected at all.
    configured: bool
    connected: bool
    # The Google account it's connected to.
    email: str | None = None


class GoogleConnectRequest(CamelModel):
    # The SPA path to come back to (unsafe values become `/`).
    return_to: Annotated[str, Field(max_length=2048)] | None = None


class GoogleConnectRedirect(CamelModel):
    """Where the SPA sends the browser to connect Google. The response also sets the state cookie."""

    authorize_url: str


class GaPropertyRead(CamelModel):
    id: str
    name: str


class GaAccountRead(CamelModel):
    id: str
    name: str
    properties: list[GaPropertyRead]


class GaStreamRead(CamelModel):
    id: str
    name: str
    measurement_id: str
    url: str | None


class SiteAnalyticsLink(CamelModel):
    """The GA4 property and web stream a site reports to."""

    property_id: GaId
    stream_id: GaId


class SiteAnalyticsRead(CamelModel):
    property_id: str
    property_name: str
    stream_id: str
    stream_name: str
    measurement_id: str
    stream_url: str | None
    time_zone: str
    # The site was last published with this stream's tag: visits are being counted. Until then the tag isn't on the
    # live site.
    tag_published: bool


class SiteAnalyticsStatus(CamelModel):
    connection: GoogleConnection
    link: SiteAnalyticsRead | None
    # Where to make a property or web stream in Google Analytics.
    create_url: str


class AnalyticsTotals(CamelModel):
    users: int
    new_users: int
    sessions: int
    page_views: int
    average_session_seconds: float
    # Fractions (0.25 is 25%).
    bounce_rate: float
    engagement_rate: float


class AnalyticsDay(CamelModel):
    date: date
    users: int
    sessions: int
    page_views: int


class AnalyticsCount(CamelModel):
    name: str
    value: int


class AnalyticsReport(CamelModel):
    """A site's traffic over `range`, in its property's time zone, for its web stream only."""

    range: ReportRange
    start_date: date
    end_date: date
    totals: AnalyticsTotals
    # The same number of days just before, to compare with.
    previous: AnalyticsTotals
    # Every day of the range, oldest first (days without visits are zeros).
    daily: list[AnalyticsDay]
    # Page paths by views.
    top_pages: list[AnalyticsCount]
    # Sessions by channel (Direct, Organic Search...).
    sources: list[AnalyticsCount]
    # Users by country and by device category.
    countries: list[AnalyticsCount]
    devices: list[AnalyticsCount]
    # Users in the last 30 minutes.
    active_now: int
    # When the numbers were read from Google (they're kept a few minutes).
    fetched_at: datetime
