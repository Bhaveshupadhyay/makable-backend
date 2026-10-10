"""The user's private workspace repo, where makable keeps their builder sessions (chat, site draft, AI history)."""

WORKSPACE_REPO = "makable-workspace"
WORKSPACE_DESCRIPTION = "Your makable chat history and site drafts. Private; managed by makable."
# Proves makable created the repo. makable never writes to a repo without it.
MARKER_PATH = ".makable-workspace.json"
MARKER_CONTENT = '{\n  "managedBy": "makable",\n  "version": 1\n}\n'
PROJECTS_DIR = "projects"


def session_path(project_id: str) -> str:
    return f"{PROJECTS_DIR}/{project_id}/session.json"


# The same caps as the SPA's `@makable/shared` `session.ts`.
SESSION_FORMAT = "makable-session"
SESSION_VERSION = 1
MAX_SESSION_BYTES = 5_000_000
MAX_MESSAGES = 2000
MAX_MESSAGE_CHARS = 10_000
MAX_STORED_TURNS = 50
# Sessions read when looking for the newest one (one per site).
MAX_PROJECTS = 20
# How long (seconds) a checked workspace repo (exists, private, has the marker) is trusted before checking again.
# Saves in between take one request instead of three. A failed write throws the check away early.
VERIFIED_REPO_TTL = 600
# Waits (seconds) for a repo GitHub just created to accept writes.
NEW_REPO_RETRY_DELAYS = (0.5, 1.0, 2.0, 4.0)
