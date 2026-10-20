from datetime import UTC, date, datetime

from app.constants.analytics import ReportRange
from app.schemas.analytics import GaReport, GaRow
from app.services.analytics_reports import (
    ReportDates,
    TtlCache,
    build_report,
    live_visitors,
    realtime_request,
    report_requests,
)

# 23:30 UTC is already the next day in India.
NOW = datetime(2026, 10, 19, 23, 30, tzinfo=UTC)


def row(*dimensions: str, metrics: list[str]) -> GaRow:
    return GaRow(dimensions=list(dimensions), metrics=metrics)


def test_the_days_are_the_property_s_days() -> None:
    utc = ReportDates(ReportRange.LAST_7_DAYS, "Etc/UTC", NOW)
    india = ReportDates(ReportRange.LAST_7_DAYS, "Asia/Kolkata", NOW)

    assert (utc.start, utc.end) == (date(2026, 10, 13), date(2026, 10, 19))
    assert (utc.previous_start, utc.previous_end) == (date(2026, 10, 6), date(2026, 10, 12))
    assert (india.start, india.end) == (date(2026, 10, 14), date(2026, 10, 20))
    assert len(ReportDates(ReportRange.LAST_90_DAYS, "Etc/UTC", NOW).days()) == 90


def test_an_unknown_time_zone_is_utc() -> None:
    assert ReportDates(ReportRange.LAST_28_DAYS, "Mars/Olympus", NOW).end == date(2026, 10, 19)


def test_every_report_is_of_the_site_s_stream_only() -> None:
    dates = ReportDates(ReportRange.LAST_28_DAYS, "Etc/UTC", NOW)
    requests = [*report_requests("42", dates), realtime_request("42")]

    stream = {"filter": {"fieldName": "streamId", "stringFilter": {"matchType": "EXACT", "value": "42"}}}
    assert all(r["dimensionFilter"] == stream for r in requests)
    current, previous, daily, pages, *_ = requests
    assert current["dateRanges"] == [{"startDate": "2026-09-22", "endDate": "2026-10-19"}]
    assert previous["dateRanges"] == [{"startDate": "2026-08-25", "endDate": "2026-09-21"}]
    assert daily["dimensions"] == [{"name": "date"}] and daily["limit"] == 28
    assert pages["dimensions"] == [{"name": "pagePath"}]
    assert pages["orderBys"] == [{"metric": {"metricName": "screenPageViews"}, "desc": True}]
    assert len(requests) == 8


def test_builds_the_report_and_fills_the_days_without_visits() -> None:
    dates = ReportDates(ReportRange.LAST_7_DAYS, "Etc/UTC", NOW)
    reports = [
        GaReport(rows=[row(metrics=["120", "100", "150", "400", "65.5", "0.4", "0.6"])]),
        GaReport(rows=[]),
        GaReport(rows=[row("20261014", metrics=["10", "12", "30"]), row("20261019", metrics=["5", "6", "9"])]),
        GaReport(rows=[row("/", metrics=["300"]), row("/menu", metrics=["100"])]),
        GaReport(rows=[row("Organic Search", metrics=["90"]), row("Direct", metrics=["60"])]),
        GaReport(rows=[row("", metrics=["2"])]),
        GaReport(rows=[row("mobile", metrics=["80"])]),
    ]

    report = build_report(ReportRange.LAST_7_DAYS, dates, reports, fetched_at=NOW)

    assert report.totals.users == 120 and report.totals.page_views == 400
    assert report.totals.average_session_seconds == 65.5 and report.totals.bounce_rate == 0.4
    assert report.previous.users == 0 and report.previous.sessions == 0
    assert [(d.date.day, d.users, d.page_views) for d in report.daily] == [
        (13, 0, 0),
        (14, 10, 30),
        (15, 0, 0),
        (16, 0, 0),
        (17, 0, 0),
        (18, 0, 0),
        (19, 5, 9),
    ]
    assert [(c.name, c.value) for c in report.top_pages] == [("/", 300), ("/menu", 100)]
    assert report.sources[0].name == "Organic Search"
    assert report.countries[0].name == "(not set)"
    assert report.devices[0].value == 80


def test_live_visitors_are_zero_without_a_row() -> None:
    assert live_visitors(GaReport(rows=[])) == 0
    assert live_visitors(GaReport(rows=[row(metrics=["4"])])) == 4


def test_the_cache_forgets_old_entries_and_keeps_its_size() -> None:
    now = [0.0]
    cache: TtlCache[str, int] = TtlCache(10, max_entries=2, clock=lambda: now[0])
    cache.put("a", 1)
    cache.put("b", 2)
    cache.put("c", 3)

    assert (cache.get("a"), cache.get("b"), cache.get("c")) == (None, 2, 3)
    now[0] = 10
    assert cache.get("b") is None
