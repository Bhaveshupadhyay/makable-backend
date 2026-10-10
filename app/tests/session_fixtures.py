"""A builder session split into workspace files, as the SPA saves it. Shared by the workspace tests."""

import json
from typing import Any
from uuid import UUID

from app.tests.ai_edit_fixtures import PORTFOLIO

PROJECT_ID = UUID("3f2a9c4e-8b1d-4c6a-9e2f-1a2b3c4d5e6f")


def state_body(
    project_id: UUID = PROJECT_ID, saved_at: str = "2026-10-10T12:00:00.000Z", **over: Any
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "format": "makable-workspace",
        "version": 1,
        "savedAt": saved_at,
        "login": "octocat",
        "projectId": str(project_id),
        "conversation": {"step": "done", "portfolio": PORTFOLIO},
        "messageChunks": 1,
        "files": {"minimal": ["src/index.css"]},
        "aiHistory": ["minimal"],
    }
    return {**body, **over}


def chunk(*texts: str) -> str:
    """A message chunk file's content."""
    messages = [{"id": f"m{i}", "role": "user", "text": text} for i, text in enumerate(texts, start=1)]
    return json.dumps({"messages": messages})


TURN = {"id": "t1", "instruction": "Make it red", "target": None, "reply": "Made it red."}
AI_HISTORY = json.dumps([TURN])

FIRST_PARTS: list[dict[str, str]] = [
    {"path": "messages/0001.json", "content": chunk("I want a portfolio", "Use Minimal")},
    {"path": "ai-history/minimal.json", "content": AI_HISTORY},
    {"path": "files/minimal/src/index.css", "content": "a { color: red }\n"},
]


def save_body(
    base_sha: str | None = None,
    parts: list[dict[str, str]] | None = None,
    deletes: list[str] | None = None,
    **state_over: Any,
) -> dict[str, Any]:
    return {
        "baseSha": base_sha,
        "state": state_body(**state_over),
        "parts": FIRST_PARTS if parts is None else parts,
        "deletes": deletes or [],
    }
