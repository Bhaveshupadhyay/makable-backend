"""A portfolio and its content file, for the legacy template sites (workspace sync and Publish's file builder)."""

import json
from typing import Any

PORTFOLIO: dict[str, Any] = {
    "profile": {"name": "Ada", "headline": "Engineer", "bio": "", "location": "", "avatarUrl": ""},
    "links": {"github": "https://github.com/ada", "linkedin": "", "x": "", "website": "", "email": ""},
    "skills": ["TypeScript"],
    "projects": [],
    "template": "minimal",
}


def render_portfolio_source(portfolio: dict[str, Any]) -> str:
    """What the SPA's `renderPortfolioSource(portfolio, 'react')` writes."""
    header = (
        "// All copy on the site lives here. Components read it and tag elements with\n"
        '// `data-content="<path>"` so the builder\'s visual editor can patch this file.'
    )
    body = json.dumps(portfolio, indent=2)
    return f"import type {{ Portfolio }} from './types'\n\n{header}\nexport const portfolio: Portfolio = {body}\n"
