"""Limits and rules for creating a site from a prompt (the create agent)."""

import re

MAX_PROMPT = 4000
# The create agent's thread for a site: `<projectId>:create` (the edit agent's is the `projectId`).
CREATE_THREAD_SUFFIX = ":create"

# Times the user can be asked for another name when the one they gave is taken on their GitHub.
MAX_NAME_TRIES = 3
# Tries the model gets at reading the user's answer to the name question (a reply that isn't the JSON asked for goes
# back once), and the names it may suggest when the user asks for some.
MAX_NAME_READ_ATTEMPTS = 2
MAX_NAME_SUGGESTIONS = 3
# Questions the model may ask about the site before it's planned (what kind of site, its sections, its look; a
# message about a question doesn't count), and its tries at each reply.
MAX_BRIEF_QUESTIONS = 3
MAX_BRIEF_ATTEMPTS = 2

# What the model may write: TypeScript, TSX and CSS files under src/ (the starter's main.tsx stays), at most this many,
# each at most this long.
CREATE_PATH = re.compile(r"src/(?!main\.tsx$)[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*\.(tsx|ts|css)")
MAX_CREATE_FILES = 14
MAX_CREATED_FILE_CHARS = 40_000
# Tries per file (a reply that isn't a file, or doesn't parse, is asked for again once), rounds of fixes after the
# static checks, and tries at the plan.
MAX_FILE_ATTEMPTS = 2
MAX_FIX_ROUNDS = 2
MAX_PLAN_ATTEMPTS = 2
# How much of the line that doesn't parse a file's rejection quotes (with the error alone, the model sends the same
# file back).
REJECTED_LINE_CHARS = 200
# Files the model wrote earlier in the run are shown whole to the next ones up to this size, else as an outline.
SHOWN_WHOLE_CHARS = 5_000

# npm packages the site may import: what the starter's package.json has (the preview fetches each from a CDN).
ALLOWED_PACKAGES = frozenset({"react", "react-dom", "react-dom/client", "react/jsx-runtime", "lucide-react"})

# The steps a creation keeps for the chat between two questions (or the last one and the end): what it did, shown
# collapsed once it's done.
MAX_STEPS = 60

# A creation streams progress; with nothing to say, it sends a ping this often (seconds), so proxies keep the
# connection open while the model writes a file.
HEARTBEAT_SECONDS = 15
