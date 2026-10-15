"""A small React project and an "Edit with AI" request on it, shared by the AI edit tests."""

import json
from collections.abc import Callable
from typing import Any
from uuid import UUID

from app.schemas.ai_edit import AiEditRequest
from app.services.ai_edit_service import AiEditService

PORTFOLIO: dict[str, Any] = {
    "profile": {"name": "Ada", "headline": "Engineer", "bio": "", "location": "", "avatarUrl": ""},
    "links": {"github": "https://github.com/ada", "linkedin": "", "x": "", "website": "", "email": ""},
    "skills": ["TypeScript"],
    "projects": [],
    "template": "minimal",
}
HERO = 'export function Hero() {\n  return <a className="bg-blue-600">Contact</a>\n}\n'
HERO_PATH = "src/components/Hero.tsx"
CONTENT_PATH = "src/content/portfolio.ts"
PROJECT_ID = UUID("6f1d3c2a-5b4e-4f7a-9c8d-1e2f3a4b5c6d")


def render_portfolio_source(portfolio: dict[str, Any]) -> str:
    """What the SPA's `renderPortfolioSource(portfolio, 'react')` writes."""
    header = (
        "// All copy on the site lives here. Components read it and tag elements with\n"
        '// `data-content="<path>"` so the builder\'s visual editor can patch this file.'
    )
    body = json.dumps(portfolio, indent=2)
    return f"import type {{ Portfolio }} from './types'\n\n{header}\nexport const portfolio: Portfolio = {body}\n"


def request_body(**overrides: Any) -> dict[str, Any]:
    """The JSON the SPA sends, camelCase."""
    body: dict[str, Any] = {
        "projectId": str(PROJECT_ID),
        "instruction": "Make the contact button green",
        "target": {
            "tag": "a",
            "text": "Contact",
            "contentPath": None,
            "section": {"tag": "section", "id": "hero", "heading": "Ada"},
            "selector": "main > section#hero > a",
            "html": '<a class="bg-blue-600">Contact</a>',
        },
        "template": {"id": "minimal", "name": "Minimal", "kind": "react", "version": 1, "contentPath": CONTENT_PATH},
        "fileTree": [HERO_PATH, CONTENT_PATH, "src/index.css"],
        "files": [
            {"path": HERO_PATH, "content": HERO, "reason": "matches the selection"},
            {"path": CONTENT_PATH, "content": render_portfolio_source(PORTFOLIO), "reason": "all copy"},
            {"path": "src/index.css", "content": ":root { --bg: #fff; }\n", "reason": "styles"},
        ],
    }
    return {**body, **overrides}


def make_request(**overrides: Any) -> AiEditRequest:
    return AiEditRequest.model_validate(request_body(**overrides))


GOOD_EDITS = [{"path": HERO_PATH, "search": "bg-blue-600", "replace": "bg-emerald-600"}]
GOOD_REPLY = json.dumps({"summary": "Made it green.", "edits": GOOD_EDITS})

# The `ai_edit_service` fixture: builds the service for a model source (and `debug`).
type MakeAiEditService = Callable[..., AiEditService]
