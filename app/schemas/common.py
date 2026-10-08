"""Shared schema bases and the response envelopes every endpoint uses."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


class CamelModel(BaseModel):
    """snake_case in Python, camelCase on the wire (the SPA's convention)."""

    model_config = ConfigDict(alias_generator=to_camel, validate_by_name=True, from_attributes=True)


class ApiResponse[T](CamelModel):
    """Success envelope: `{"success": true, "data": ...}`."""

    success: Literal[True] = True
    data: T


class ErrorDetail(CamelModel):
    code: str
    message: str
    details: Any = None


class ErrorResponse(CamelModel):
    """Error envelope: `{"success": false, "error": {...}, "requestId": ...}`."""

    success: Literal[False] = False
    error: ErrorDetail
    request_id: str | None = None
