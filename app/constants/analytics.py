"""Google Analytics: users connect their Google account (read-only), pick the GA4 property and web stream a site
reports to, and the site's deploy adds Google's tag. Reports are read with their token from Google's APIs."""

import re
from enum import StrEnum

from app.constants.api import API_V1_PREFIX

# Only these fixed hosts are called.
GOOGLE_AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_REVOKE_URL = "https://oauth2.googleapis.com/revoke"
GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
ANALYTICS_ADMIN_API = "https://analyticsadmin.googleapis.com/v1beta"
ANALYTICS_DATA_API = "https://analyticsdata.googleapis.com/v1beta"

# Read-only: list the user's properties and streams, and run reports. `openid email` names the connected account.
ANALYTICS_SCOPE = "https://www.googleapis.com/auth/analytics.readonly"
GOOGLE_SCOPES = f"openid email {ANALYTICS_SCOPE}"

# The connect step's PKCE verifier, state and return path, only sent to the callback.
GOOGLE_STATE_COOKIE = "makable_google_state"
GOOGLE_STATE_COOKIE_PATH = f"{API_V1_PREFIX}/analytics/google"
GOOGLE_CALLBACK_PATH = f"{API_V1_PREFIX}/analytics/google/callback"

GOOGLE_TIMEOUT_SECONDS = 20

# A GA4 measurement id (`G-1A2BCD345E`). It goes into the deploy workflow and the site's pages, so it's checked.
MEASUREMENT_ID = re.compile(r"G-[A-Z0-9]{4,20}")
# GA's resource ids are numbers.
GA_ID = re.compile(r"[0-9]{1,20}")

# Pages of `accountSummaries` (200 accounts each) read at most.
MAX_ACCOUNT_PAGES = 5


class ReportRange(StrEnum):
    LAST_7_DAYS = "7d"
    LAST_28_DAYS = "28d"
    LAST_90_DAYS = "90d"


REPORT_DAYS = {ReportRange.LAST_7_DAYS: 7, ReportRange.LAST_28_DAYS: 28, ReportRange.LAST_90_DAYS: 90}

# Rows in each "top" list (pages, sources, countries, devices).
TOP_ROWS = 10

# Reports are kept per worker this long (GA's numbers are hours behind anyway), and live visitors this long.
REPORT_CACHE_SECONDS = 10 * 60
REALTIME_CACHE_SECONDS = 60
MAX_CACHED_REPORTS = 1000

# Requests that call Google, at once per worker.
MAX_CONCURRENT_ANALYTICS_REQUESTS = 50
# Property and stream lists per user (uncached): bursts of this many, then one per this many seconds.
ANALYTICS_LIST_BURST = 20
ANALYTICS_LIST_EVERY_SECONDS = 3

# Where the user makes a property or a web stream when they have none.
GA_CREATE_URL = "https://analytics.google.com/analytics/web/#/provision"
