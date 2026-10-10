"""The user's private workspace repo, where makable keeps their builder sessions (chat, site draft, AI history)."""

WORKSPACE_REPO = "makable-workspace"
WORKSPACE_DESCRIPTION = "Your makable chat history and site drafts. Private; managed by makable."
# Proves makable created the repo. makable never writes to a repo without it.
MARKER_PATH = ".makable-workspace.json"
MARKER_CONTENT = '{\n  "managedBy": "makable",\n  "version": 1\n}\n'
PROJECTS_DIR = "projects"


def project_dir(project_id: str) -> str:
    return f"{PROJECTS_DIR}/{project_id}"


# A site's session is split into files, so a save only writes what changed (paths are inside the project dir):
#   state.json            the small part that changes often (written by the server on every save)
#   messages/0001.json    the chat in chunks; full chunks never change again
#   ai-history/<template>.json  the AI turns for a template
#   files/<template>/<path>     each AI-edited file, as plain text
STATE_FILE = "state.json"
STATE_FORMAT = "makable-workspace"
STATE_VERSION = 1
MAX_CHUNKS = 9999
CHUNK_MAX_MESSAGES = 200
# The SPA closes a chunk at about 48 KB; a JSON-escaped message can be several times its 10,000 characters.
MAX_JSON_PART_CHARS = 200_000
# 50 turns of at most about 3,300 characters, JSON-escaped.
MAX_AI_HISTORY_CHARS = 400_000
MAX_PARTS_PER_SAVE = 20
MAX_DELETES_PER_SAVE = 500
MAX_FILES_PER_TEMPLATE = 500


# The same caps as the SPA's `@makable/shared` `session.ts`.
MAX_MESSAGE_CHARS = 10_000
MAX_STORED_TURNS = 50
# States read when looking for the newest session (one per site).
MAX_PROJECTS = 20
# How long (seconds) a checked workspace repo (exists, private, has the marker) is trusted before checking again.
# Saves in between take one request instead of three. A failed write throws the check away early.
VERIFIED_REPO_TTL = 600
# Waits (seconds) for a repo GitHub just created to accept writes.
NEW_REPO_RETRY_DELAYS = (0.5, 1.0, 2.0, 4.0)
