"""A builder session file as the SPA writes it (`toSnapshot`), shared by the workspace tests."""

from typing import Any
from uuid import UUID

from app.tests.ai_edit_fixtures import PORTFOLIO

PROJECT_ID = UUID("3f2a9c4e-8b1d-4c6a-9e2f-1a2b3c4d5e6f")


def snapshot_body(
    project_id: UUID = PROJECT_ID, exported_at: str = "2026-10-10T12:00:00.000Z", **over: Any
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "format": "makable-session",
        "version": 1,
        "exportedAt": exported_at,
        "login": "octocat",
        "projectId": str(project_id),
        "conversation": {
            "step": "done",
            "messages": [
                {"id": "m1", "role": "assistant", "text": "Hi!", "replies": ["I want a portfolio"]},
                {"id": "m2", "role": "user", "text": "I want a portfolio"},
            ],
            "portfolio": PORTFOLIO,
        },
        "fileEdits": {"minimal": {"src/index.css": "a { color: red }"}},
        "aiHistory": {"minimal": [{"id": "t1", "instruction": "Make it red", "target": None, "reply": "Made it red."}]},
    }
    return {**body, **over}
