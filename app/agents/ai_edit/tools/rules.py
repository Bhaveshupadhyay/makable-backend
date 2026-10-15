"""Rules that send a request straight to Tier 2, without a model call."""

from app.constants.ai_edit import SITE_WIDE
from app.schemas.ai_edit import AiEditRequest


def tier_2_rule(request: AiEditRequest) -> str | None:
    """The reason a request goes straight to Tier 2 without a model call, or None for Tier 1."""
    if not request.files:
        return "No file was found for this change."
    match = SITE_WIDE.search(request.instruction)
    if match:
        return f'"{match.group(0)}" sounds like a change across the site.'
    return None
