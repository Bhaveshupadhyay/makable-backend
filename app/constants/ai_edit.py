"""Limits and rules for AI edits. The caps mirror the SPA's contract (`@makable/shared` `ai-edit.ts` and
`ai-edit-files.ts`), so a request the SPA builds always validates here."""

import re
from enum import StrEnum

MAX_INSTRUCTION = 2000
MAX_ELEMENT_HTML = 4000
MAX_FILE_CHARS = 60_000
MAX_FILES = 6
MAX_FILE_TREE = 500
MAX_EDITS = 10
# Earlier requests that go into the prompt (the last ones of the thread). The browser may still send its own
# `history`, which is validated with the same caps and then ignored: the server keeps the thread now.
MAX_HISTORY = 10
MAX_HISTORY_REPLY = 1000
MAX_TARGET_LABEL = 300

# A relative path inside a template: no leading slash, no `.`/`..` segments, no backslashes. The same rule as
# the SPA's `SAFE_REPO_PATH`, with `\w` spelled out because Python's is Unicode-aware and JavaScript's isn't.
SAFE_REPO_PATH = re.compile(r"(?!.*(?:^|/)\.{1,2}(?:/|$))[A-Za-z0-9_.@-]+(?:/[A-Za-z0-9_.@-]+)*")

# Files the model may never edit, even when they were sent.
FORBIDDEN_PATH = re.compile(
    r"(^|/)(package\.json|package-lock\.json|bun\.lockb?|yarn\.lock|pnpm-lock\.yaml)$|^\.github/"
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
