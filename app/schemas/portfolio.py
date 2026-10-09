"""The site's content (`@makable/shared` `portfolio.ts`). Used to check that an AI edit kept the content file
valid. Strict, like the SPA's zod schema: no type coercion."""

import re
from typing import Annotated
from urllib.parse import urlsplit

from pydantic import AfterValidator, ConfigDict, Field

from app.schemas.common import CamelModel

# zod's default email pattern, so both sides accept the same addresses.
EMAIL = re.compile(r"(?!\.)(?!.*\.\.)[A-Za-z0-9_'+\-.]*[A-Za-z0-9_+-]@(?:[A-Za-z0-9][A-Za-z0-9\-]*\.)+[A-Za-z]{2,}")


def _http_url(value: str) -> str:
    # http(s) only: these end up in `href`s on the published site, so no `javascript:` URLs.
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.hostname or any(c.isspace() for c in value):
        raise ValueError("Enter an http(s) URL")
    return value


def _optional_url(value: str) -> str:
    return value if value == "" else _http_url(value)


def _optional_email(value: str) -> str:
    if value != "" and not EMAIL.fullmatch(value):
        raise ValueError("Invalid email address")
    return value


HttpUrl = Annotated[str, AfterValidator(_http_url)]
OptionalUrl = Annotated[str, AfterValidator(_optional_url)]
TemplateId = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")]
NonEmpty = Annotated[str, Field(min_length=1)]


class StrictModel(CamelModel):
    model_config = ConfigDict(strict=True)


class PortfolioProfile(StrictModel):
    name: NonEmpty
    headline: NonEmpty
    bio: str
    location: str
    avatar_url: OptionalUrl


class PortfolioLinks(StrictModel):
    github: OptionalUrl
    linkedin: OptionalUrl
    x: OptionalUrl
    website: OptionalUrl
    email: Annotated[str, AfterValidator(_optional_email)]


class PortfolioProject(StrictModel):
    name: NonEmpty
    description: str
    repo_url: HttpUrl
    homepage_url: OptionalUrl
    language: str | None
    stars: Annotated[int, Field(ge=0)]


class Portfolio(StrictModel):
    profile: PortfolioProfile
    links: PortfolioLinks
    skills: list[NonEmpty]
    projects: list[PortfolioProject]
    template: TemplateId
