"""What a site's traffic report asks Google Analytics for, how GA's answers become an `AnalyticsReport`, and the
per-worker caches that keep a dashboard's reloads from using up the property's quota."""

import time
from collections.abc import Callable, Hashable
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.constants.analytics import (
    MAX_CACHED_REPORTS,
    REALTIME_CACHE_SECONDS,
    REPORT_CACHE_SECONDS,
    REPORT_DAYS,
    TOP_ROWS,
    ReportRange,
)
from app.schemas.analytics import AnalyticsCount, AnalyticsDay, AnalyticsReport, AnalyticsTotals, GaReport, GaRow

# The totals' metrics, in `AnalyticsTotals`' order.
TOTAL_METRICS = (
    "activeUsers",
    "newUsers",
    "sessions",
    "screenPageViews",
    "averageSessionDuration",
    "bounceRate",
    "engagementRate",
)
DAILY_METRICS = ("activeUsers", "sessions", "screenPageViews")
# The "top" lists: (dimension, the metric they're sorted by).
TOP_LISTS = (
    ("pagePath", "screenPageViews"),
    ("sessionDefaultChannelGroup", "sessions"),
    ("country", "activeUsers"),
    ("deviceCategory", "activeUsers"),
)


class TtlCache[K: Hashable, V]:
    """Values kept for `ttl` seconds, at most `max_entries` (the oldest go first). Per worker."""

    def __init__(
        self, ttl: float, *, max_entries: int = MAX_CACHED_REPORTS, clock: Callable[[], float] = time.monotonic
    ):
        self._ttl = ttl
        self._max = max_entries
        self._clock = clock
        self._entries: dict[K, tuple[V, float]] = {}

    def get(self, key: K) -> V | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        if entry[1] <= self._clock():
            del self._entries[key]
            return None
        return entry[0]

    def put(self, key: K, value: V) -> None:
        now = self._clock()
        self._entries.pop(key, None)
        if len(self._entries) >= self._max:
            self._entries = {k: e for k, e in self._entries.items() if e[1] > now}
            while len(self._entries) >= self._max:
                del self._entries[next(iter(self._entries))]
        self._entries[key] = (value, now + self._ttl)

    def clear(self) -> None:
        self._entries.clear()


# (user id, property id, stream id, range) -> the report without `active_now`.
ANALYTICS_REPORTS: TtlCache[tuple[object, ...], AnalyticsReport] = TtlCache(REPORT_CACHE_SECONDS)
# (user id, property id, stream id) -> users in the last 30 minutes.
LIVE_VISITORS: TtlCache[tuple[object, ...], int] = TtlCache(REALTIME_CACHE_SECONDS)


class ReportDates:
    """The range's days and the same number of days before them, in the property's time zone."""

    def __init__(self, range_: ReportRange, time_zone: str, now: datetime) -> None:
        days = REPORT_DAYS[range_]
        self.end = now.astimezone(_zone(time_zone)).date()
        self.start = self.end - timedelta(days=days - 1)
        self.previous_end = self.start - timedelta(days=1)
        self.previous_start = self.previous_end - timedelta(days=days - 1)

    def days(self) -> list[date]:
        return [self.start + timedelta(days=n) for n in range((self.end - self.start).days + 1)]


def report_requests(stream_id: str, dates: ReportDates) -> list[dict[str, Any]]:
    """The Data API reports behind an `AnalyticsReport`, all on the site's stream only: the totals now and before, the
    days, then the top lists."""
    current = [_date_range(dates.start, dates.end)]
    stream = _stream_filter(stream_id)
    return [
        {"dateRanges": current, "metrics": _metrics(TOTAL_METRICS), "dimensionFilter": stream},
        {
            "dateRanges": [_date_range(dates.previous_start, dates.previous_end)],
            "metrics": _metrics(TOTAL_METRICS),
            "dimensionFilter": stream,
        },
        {
            "dateRanges": current,
            "dimensions": [{"name": "date"}],
            "metrics": _metrics(DAILY_METRICS),
            "dimensionFilter": stream,
            "orderBys": [{"dimension": {"dimensionName": "date"}}],
            "limit": len(dates.days()),
        },
        *(
            {
                "dateRanges": current,
                "dimensions": [{"name": dimension}],
                "metrics": [{"name": metric}],
                "dimensionFilter": stream,
                "orderBys": [{"metric": {"metricName": metric}, "desc": True}],
                "limit": TOP_ROWS,
            }
            for dimension, metric in TOP_LISTS
        ),
    ]


def realtime_request(stream_id: str) -> dict[str, Any]:
    """Users on the site's stream in the last 30 minutes."""
    return {"metrics": [{"name": "activeUsers"}], "dimensionFilter": _stream_filter(stream_id)}


def build_report(
    range_: ReportRange, dates: ReportDates, reports: list[GaReport], *, fetched_at: datetime
) -> AnalyticsReport:
    """The report from the answers to `report_requests`, in their order. `active_now` is filled in separately."""
    totals, previous, daily, pages, sources, countries, devices = reports
    by_day = {_date(row.dimensions[0]): row.metrics for row in daily.rows if row.dimensions}
    return AnalyticsReport(
        range=range_,
        start_date=dates.start,
        end_date=dates.end,
        totals=_totals(totals),
        previous=_totals(previous),
        daily=[
            AnalyticsDay(
                date=day,
                users=_int(by_day.get(day), 0),
                sessions=_int(by_day.get(day), 1),
                page_views=_int(by_day.get(day), 2),
            )
            for day in dates.days()
        ],
        top_pages=_counts(pages),
        sources=_counts(sources),
        countries=_counts(countries),
        devices=_counts(devices),
        active_now=0,
        fetched_at=fetched_at,
    )


def live_visitors(report: GaReport) -> int:
    return _int(report.rows[0].metrics if report.rows else None, 0)


def _totals(report: GaReport) -> AnalyticsTotals:
    # A report with no data has no row.
    values = report.rows[0].metrics if report.rows else None
    return AnalyticsTotals(
        users=_int(values, 0),
        new_users=_int(values, 1),
        sessions=_int(values, 2),
        page_views=_int(values, 3),
        average_session_seconds=_float(values, 4),
        bounce_rate=_float(values, 5),
        engagement_rate=_float(values, 6),
    )


def _counts(report: GaReport) -> list[AnalyticsCount]:
    return [
        AnalyticsCount(name=_name(row), value=_int(row.metrics, 0)) for row in report.rows[:TOP_ROWS] if row.dimensions
    ]


def _name(row: GaRow) -> str:
    # GA writes "(not set)" for visits it couldn't place (a country it couldn't tell, say).
    return row.dimensions[0] or "(not set)"


def _float(values: list[str] | None, index: int) -> float:
    try:
        return float(values[index]) if values is not None else 0.0
    except IndexError, ValueError:
        return 0.0


def _int(values: list[str] | None, index: int) -> int:
    return round(_float(values, index))


def _date(value: str) -> date | None:
    try:
        return datetime.strptime(value, "%Y%m%d").date()
    except ValueError:
        return None


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError, ValueError:
        return ZoneInfo("UTC")


def _date_range(start: date, end: date) -> dict[str, str]:
    return {"startDate": start.isoformat(), "endDate": end.isoformat()}


def _metrics(names: tuple[str, ...]) -> list[dict[str, str]]:
    return [{"name": name} for name in names]


def _stream_filter(stream_id: str) -> dict[str, Any]:
    """Only the site's web stream: a property can have other sites' streams."""
    return {"filter": {"fieldName": "streamId", "stringFilter": {"matchType": "EXACT", "value": stream_id}}}
