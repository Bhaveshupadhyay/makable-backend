"""Publishing a site: its repo gets a deploy workflow for the hosting platform the user picked, which builds the site
and deploys it when makable starts it."""

import re
from enum import StrEnum
from typing import Literal

# How a site is built: React (Vite) when it has src/main.tsx, else plain files served as they are.
SiteKind = Literal["react", "static"]


class PublishTarget(StrEnum):
    GITHUB_PAGES = "github-pages"
    VERCEL = "vercel"
    CLOUDFLARE_PAGES = "cloudflare-pages"
    NETLIFY = "netlify"


# The Actions secrets each platform's workflow reads. The names are what the user types on GitHub when they add
# the secrets themselves, so they're the platforms' usual names.
TARGET_SECRETS: dict[PublishTarget, tuple[str, ...]] = {
    PublishTarget.GITHUB_PAGES: (),
    PublishTarget.VERCEL: ("VERCEL_TOKEN",),
    PublishTarget.CLOUDFLARE_PAGES: ("CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID"),
    PublishTarget.NETLIFY: ("NETLIFY_AUTH_TOKEN",),
}

# Where the user makes a token, for the chat to link to.
TARGET_TOKEN_URLS: dict[PublishTarget, str | None] = {
    PublishTarget.GITHUB_PAGES: None,
    PublishTarget.VERCEL: "https://vercel.com/account/tokens",
    PublishTarget.CLOUDFLARE_PAGES: "https://dash.cloudflare.com/profile/api-tokens",
    PublishTarget.NETLIFY: "https://app.netlify.com/user/applications#personal-access-tokens",
}

TARGET_NAMES: dict[PublishTarget, str] = {
    PublishTarget.GITHUB_PAGES: "GitHub Pages",
    PublishTarget.VERCEL: "Vercel",
    PublishTarget.CLOUDFLARE_PAGES: "Cloudflare Pages",
    PublishTarget.NETLIFY: "Netlify",
}

WORKFLOW_FILE = "makable-deploy.yml"
WORKFLOW_PATH = f".github/workflows/{WORKFLOW_FILE}"

# A project name every platform accepts (Cloudflare Pages allows the fewest characters: 58).
PROJECT_NAME = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,56}[a-z0-9])?")
PROJECT_NAME_MAX = 58
MAX_TOKEN_CHARS = 500
# Waits (seconds) for GitHub to accept starting a workflow it just received (a new or changed workflow file can
# take a few seconds to register).
DISPATCH_RETRY_DELAYS = (1.0, 2.0, 4.0, 8.0)
# How far before the dispatch a deploy's start time is put, so a run GitHub dates by a clock slightly behind ours still
# counts as this deploy's.
DEPLOY_CLOCK_SKEW_SECONDS = 10
