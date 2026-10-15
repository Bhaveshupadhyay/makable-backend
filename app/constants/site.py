"""A site's code lives in a private repo on the user's GitHub, one repo per site. makable reads it as an archive at
a commit, and writes every change (the user's manual edits, AI edits, Undo) as a commit."""

import re
from typing import Final

SITE_DESCRIPTION = "My website, built with makable"
# Marks the repo as makable's, for one site (`projectId`). makable only writes to a repo whose manifest names the
# site it's writing for.
MANIFEST_PATH = ".makable/manifest.json"
MANIFEST_FORMAT: Final = "makable-site"
MANIFEST_VERSION: Final = 2
# Where makable's own files and git's live: never written by edits, and read-only in the editor.
RESERVED_PATH = re.compile(r"^(\.github|\.makable|\.git)(/|$)")

SITE_NAME_MAX = 100
# GitHub's rule for repo names, minus the names git treats specially.
REPO_NAME = re.compile(r"(?!\.{1,2}$)[A-Za-z0-9._-]{1,100}")

# A save of manual edits: files at most, characters per file, and in total (the request body is capped at 2 MB).
MAX_SAVE_FILES = 200
MAX_FILE_CHARS = 500_000
MAX_SAVE_CHARS = 1_500_000
MAX_COMMIT_MESSAGE = 200
# A save has this long (seconds) to reach its commit; the commit itself isn't cut short (see WorkspaceService).
SAVE_DEADLINE_SECONDS = 20

# Snapshots of sites at a commit (commits never change), kept in memory per worker for this many seconds, up to
# this many bytes of file contents in total.
SNAPSHOT_TTL = 600
SNAPSHOT_CACHE_BYTES = 200_000_000
# Repos checked recently (exist, have the manifest for the site) are trusted this long (seconds).
VERIFIED_SITE_TTL = 600
# Waits (seconds) for a repo GitHub just created to show its first commit.
NEW_REPO_RETRY_DELAYS = (0.5, 1.0, 2.0, 4.0)
