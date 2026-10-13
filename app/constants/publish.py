"""Publishing a site: a repo on the user's GitHub with the site's files and a deploy workflow for the hosting
platform they picked. The workflow builds the site and deploys it on every push."""

import re
from enum import StrEnum
from typing import Final


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

# Files makable owns in a site repo. The manifest marks the repo as makable's and lists every file it manages
# with the SHA it wrote, so a change made on GitHub since is noticed before it's overwritten.
MANIFEST_PATH = ".makable/manifest.json"
MANIFEST_FORMAT: Final = "makable-site"
MANIFEST_VERSION: Final = 1
WORKFLOW_FILE = "makable-deploy.yml"
WORKFLOW_PATH = f".github/workflows/{WORKFLOW_FILE}"
# Template files may not write where makable's own files live, or into git's.
RESERVED_PATH = re.compile(r"^(\.github|\.makable|\.git)(/|$)")

SITE_DESCRIPTION = "My website, built with makable"
# Non-Pages sites go to a repo with this name unless the user picks another.
DEFAULT_REPO = "portfolio"
# GitHub's rule for repo names, minus the names git treats specially.
REPO_NAME = re.compile(r"(?!\.{1,2}$)[A-Za-z0-9._-]{1,100}")
# A project name every platform accepts (Cloudflare Pages allows the fewest characters: 58).
PROJECT_NAME = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,56}[a-z0-9])?")
PROJECT_NAME_MAX = 58

# AI-edited files a published site may have (read from the saved session).
MAX_FILE_EDITS = 100
MAX_TOKEN_CHARS = 500
# The catalog itself is a short list of templates, read up to this size.
MAX_CATALOG_BYTES = 1_000_000
# A template's files.json is read up to this size. Images, video and 3D models are in it as base64, so a template
# with a lot of media is well past 10 MB.
MAX_TEMPLATE_BYTES = 50_000_000
# One binary file of a template, decoded. Each is sent to GitHub as base64 in a request of its own, which has to
# stay well inside what GitHub's API takes.
MAX_TEMPLATE_FILE_BYTES = 15_000_000
# How long (seconds) the template catalog is trusted before it's read again. A template's files are cached
# longer: their URL carries the template's version, so they don't change.
CATALOG_TTL = 300
# The templates' files kept in memory, in total (text characters plus decoded binary bytes). The least recently
# used go first. Only one template is downloaded and parsed at a time, so a worker holds this plus one load.
TEMPLATE_FILES_CACHE_BYTES = 100_000_000
# Waits (seconds) for GitHub to accept starting a workflow it just received (a new or changed workflow file can
# take a few seconds to register).
DISPATCH_RETRY_DELAYS = (1.0, 2.0, 4.0, 8.0)
# How far before the dispatch a deploy's start time is put, so a run GitHub dates by a clock slightly behind ours still
# counts as this deploy's.
DEPLOY_CLOCK_SKEW_SECONDS = 10
# Waits (seconds) for a repo GitHub just created to show its first commit.
NEW_REPO_RETRY_DELAYS = (0.5, 1.0, 2.0, 4.0)
# Paths listed in a "changed on GitHub" conflict.
MAX_CONFLICT_PATHS = 20
