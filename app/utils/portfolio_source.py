"""A template's content file: a port of `renderPortfolioSource` and `parsePortfolioSource` from `@makable/shared`
`portfolio-source.ts`. The SPA renders it for the preview and parses it back when it applies an AI edit."""

import json
import re

from pydantic import ValidationError

from app.schemas.portfolio import Portfolio
from app.schemas.template import TemplateKind

_HEADER = """// All copy on the site lives here. Components read it and tag elements with
// `data-content="<path>"` so the builder's visual editor can patch this file."""

_DECLARATION = re.compile(r"export const portfolio(?:\s*:\s*Portfolio)?\s*=\s*")


class PortfolioSourceError(ValueError):
    """The content file no longer holds a valid portfolio."""


def render_portfolio_source(portfolio: Portfolio, kind: TemplateKind) -> str:
    """The content file for a portfolio: `portfolio.ts` for React templates, `portfolio.js` for static ones. JSON is
    a valid object literal in both, so the content is the JSON itself, laid out like `JSON.stringify(_, null, 2)`."""
    content = json.dumps(portfolio.model_dump(mode="json", by_alias=True), indent=2, ensure_ascii=False)
    if kind == "react":
        header = f"import type {{ Portfolio }} from './types'\n\n{_HEADER}"
        return f"{header}\nexport const portfolio: Portfolio = {content}\n"
    return f"{_HEADER}\nexport const portfolio = {content}\n"


def parse_portfolio_source(source: str) -> Portfolio:
    """Parses `export const portfolio = {...}`. The object literal must be JSON and a valid portfolio.

    Raises:
        PortfolioSourceError: The declaration is missing, the object isn't JSON, or the content is invalid.
    """
    match = _DECLARATION.search(source)
    if not match:
        raise PortfolioSourceError("the content file must keep `export const portfolio = {...}`")
    literal = source[match.end() :].strip().removesuffix(";")
    try:
        # JSON.parse rejects NaN and Infinity, so this does too.
        data = json.loads(literal, parse_constant=_reject_constant)
    except ValueError:
        raise PortfolioSourceError(
            "the content object must stay valid JSON (double quotes, no trailing commas, no comments)"
        ) from None
    try:
        return Portfolio.model_validate(data)
    except ValidationError as err:
        issue = err.errors()[0]
        where = ".".join(str(part) for part in issue["loc"]) or "the top level"
        raise PortfolioSourceError(f"the content is invalid at {where}: {issue['msg']}") from None


def _reject_constant(name: str) -> object:
    raise ValueError(f"{name} isn't JSON")
