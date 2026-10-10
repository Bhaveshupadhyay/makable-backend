API_V1_PREFIX = "/api/v1"
REQUEST_ID_HEADER = "X-Request-ID"

# Request bodies are read up to these sizes (bytes) and refused with 413 past them, before anything is parsed.
MAX_BODY_BYTES = 64_000
# (method, path prefix, limit) for routes that take more. AI edits carry up to six 60,000-character files; a
# workspace save batch is at most ~512 KB of files, JSON-escaped.
ROUTE_BODY_LIMITS: tuple[tuple[str, str, int], ...] = (
    ("POST", f"{API_V1_PREFIX}/ai/edit", 2_000_000),
    ("PUT", f"{API_V1_PREFIX}/workspace/sessions/", 2_000_000),
)

# Requests of a kind in progress at once, per worker. Past these, a quick 503 instead of piling up in memory.
MAX_CONCURRENT_AI_EDITS = 50
MAX_CONCURRENT_WORKSPACE_REQUESTS = 100
# Workspace saves per user: bursts of up to this many (the batches of a big save), then one per this many seconds.
WORKSPACE_SAVE_BURST = 20
WORKSPACE_SAVE_EVERY_SECONDS = 15
