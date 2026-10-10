"""Limits and rules for AI edits. The caps mirror the SPA's contract (`@makable/shared` `ai-edit.ts` and
`ai-edit-files.ts`), so a request the SPA builds always validates here."""

import re

MAX_INSTRUCTION = 2000
MAX_ELEMENT_HTML = 4000
MAX_FILE_CHARS = 60_000
MAX_FILES = 6
MAX_FILE_TREE = 500
MAX_EDITS = 10
# Earlier requests the browser sends back as context (it keeps them; the server stores nothing).
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
