"""Reads a template's content file back into a portfolio: a port of `parsePortfolioSource` from
`@makable/shared` `portfolio-source.ts`. The SPA does the same when it applies an AI edit to that file."""

import json
import re

from pydantic import ValidationError

from app.schemas.portfolio import Portfolio

_DECLARATION = re.compile(r"export const portfolio(?:\s*:\s*Portfolio)?\s*=\s*")


class PortfolioSourceError(ValueError):
    """The content file no longer holds a valid portfolio."""


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
