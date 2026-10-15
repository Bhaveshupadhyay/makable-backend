"""Limits and rules for AI edits. The request's caps mirror the SPA's contract (`@makable/shared` `ai-edit.ts`), so a
request the SPA builds always validates here."""

import re
from enum import StrEnum

MAX_INSTRUCTION = 2000
MAX_ELEMENT_HTML = 4000
# The workspace's AI-edited files (legacy template sites) are capped at this size.
MAX_FILE_CHARS = 60_000
MAX_EDITS = 10
# A file an AI edit leaves (or creates) can be at most this long (JavaScript string length, like the browser).
MAX_EDITED_FILE_CHARS = 200_000
# The picture of the selected element (a data URL), sent to models that read images.
MAX_SCREENSHOT_CHARS = 400_000
SCREENSHOT = re.compile(r"data:image/(png|jpeg|webp);base64,[A-Za-z0-9+/]+=*")

# What the model sees of the site. A file up to this size is shown whole; a longer one as the lines around the
# selection (this many on each side) and an outline of the rest.
WHOLE_FILE_CHARS = 16_000
CONTEXT_WINDOW_LINES = 40
# The model can ask for more of the code (`needContext`) this many times per request, up to this many lines in all.
MAX_CONTEXT_ROUNDS = 2
MAX_EXTRA_CONTEXT_LINES = 800
# Questions the model can ask the user per request (the run pauses until they answer), and their caps.
MAX_QUESTIONS = 2
MAX_QUESTION = 300
MAX_OPTIONS = 6
MAX_OPTION = 80
MAX_ANSWER = 2000

# Times the preview can fail on the AI's edits before the request gives up (each failure goes back to the model),
# and how much of the preview's error goes back.
MAX_PREVIEW_ROUNDS = 2
MAX_PREVIEW_ERROR = 1000
PREVIEW_EXCERPT_LINES = 12

# Paths listed in the prompt's file list, and lines of a file's outline.
MAX_LISTED_FILES = 300
MAX_OUTLINE_LINES = 80
# Earlier requests that go into the prompt (the last ones of the thread). The browser may still send its own
# `history`, which is validated with the same caps and then ignored: the server keeps the thread now.
MAX_HISTORY = 10
MAX_HISTORY_REPLY = 1000
MAX_TARGET_LABEL = 300

# A relative path inside a site: no leading slash, no `.`/`..` segments, no backslashes. The same rule as
# the SPA's `SAFE_REPO_PATH`, with `\w` spelled out because Python's is Unicode-aware and JavaScript's isn't.
SAFE_REPO_PATH = re.compile(r"(?!.*(?:^|/)\.{1,2}(?:/|$))[A-Za-z0-9_.@-]+(?:/[A-Za-z0-9_.@-]+)*")

# Files the model may never edit: dependencies (the preview can't install new ones), and makable's and git's own.
FORBIDDEN_PATH = re.compile(
    r"(^|/)(package\.json|package-lock\.json|bun\.lockb?|yarn\.lock|pnpm-lock\.yaml)$|^\.(github|makable|git)/"
)

# Requests that are clearly bigger than a few files go straight to Tier 2, without a model call.
SITE_WIDE = re.compile(
    r"\b(new page|another page|add(?:ing)? a page|every page|all pages|whole site|entire site|site-?wide|everywhere"
    r"|install|npm|dependency|dependencies|router|routing|refactor)\b",
    re.IGNORECASE,
)

# Tries per request: the first answer, plus one retry that tells the model why its answer was rejected.
MAX_ATTEMPTS = 2

# Postgres schema for LangGraph's checkpoint tables. `AsyncPostgresSaver.setup()` creates them there (through the
# connection's search_path), not Alembic, so `alembic/env.py` leaves the schema out.
LANGGRAPH_SCHEMA = "langgraph"

# Finished requests kept in a thread's state (the chat), oldest dropped first. Every checkpoint stores them all.
MAX_TURNS = 200


class AiEditOutcome(StrEnum):
    """How a request ended, in `ai_edit_requests` and the thread's turns."""

    # Tier 1 edits that passed the checks (the SPA applies them).
    APPLIED = "applied"
    # Every answer was rejected by the checks.
    REJECTED = "rejected"
    # Tier 2: by rule, or the model said the change needs a deeper edit.
    ESCALATED = "escalated"
    # The model or the server failed (bad key, rate limit, provider down).
    FAILED = "failed"
    # Stopped by the user before it finished.
    CANCELLED = "cancelled"
    # A paused run that a new instruction replaced (step 3a).
    SKIPPED = "skipped"
