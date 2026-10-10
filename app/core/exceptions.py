"""Domain exceptions and the global handlers that turn them into error envelopes."""

import logging
from collections.abc import Mapping
from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.logging import request_id_var
from app.schemas.common import ErrorDetail, ErrorResponse

logger = logging.getLogger(__name__)


class AppError(Exception):
    """Base for errors that are safe to show the client. Subclasses set the status and code."""

    status_code: int = status.HTTP_500_INTERNAL_SERVER_ERROR
    code: str = "internal_error"
    message: str = "Something went wrong"

    def __init__(
        self,
        message: str | None = None,
        *,
        code: str | None = None,
        details: Any = None,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        self.message = message or self.message
        self.code = code or self.code
        self.details = details
        # Extra response headers, e.g. `Retry-After`.
        self.headers = headers
        super().__init__(self.message)


class UnauthorizedError(AppError):
    status_code = status.HTTP_401_UNAUTHORIZED
    code = "unauthorized"
    message = "Not signed in"


class ForbiddenError(AppError):
    status_code = status.HTTP_403_FORBIDDEN
    code = "forbidden"
    message = "Not allowed"


class NotFoundError(AppError):
    status_code = status.HTTP_404_NOT_FOUND
    code = "not_found"
    message = "Not found"


class ExternalServiceError(AppError):
    status_code = status.HTTP_502_BAD_GATEWAY
    code = "external_service_error"
    message = "An upstream service failed"


class ServiceUnavailableError(AppError):
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    code = "service_unavailable"
    message = "Service unavailable"


class ConflictError(AppError):
    """A write clashed with existing data (e.g. a unique constraint)."""

    status_code = status.HTTP_409_CONFLICT
    code = "conflict"
    message = "Conflicts with existing data"


class LoginError(AppError):
    """The OAuth callback couldn't sign the user in. The callback turns it into a redirect, not JSON."""

    status_code = status.HTTP_400_BAD_REQUEST
    code = "login_failed"
    message = "Login failed"

    def __init__(self, code: str, message: str | None = None, *, return_to: str = "/") -> None:
        super().__init__(message, code=code)
        self.return_to = return_to


def error_response(
    status_code: int, code: str, message: str, details: Any = None, headers: Mapping[str, str] | None = None
) -> JSONResponse:
    """The error envelope, for code that answers outside the exception handlers (e.g. middleware)."""
    body = ErrorResponse(
        error=ErrorDetail(code=code, message=message, details=details), request_id=request_id_var.get()
    )
    return JSONResponse(jsonable_encoder(body.model_dump(by_alias=True)), status_code=status_code, headers=headers)


def internal_error_response(exc: Exception) -> JSONResponse:
    """Logs an unhandled exception with its stack trace. The client only gets a generic message and the
    request id."""
    logger.exception("unhandled error", exc_info=exc)
    return error_response(status.HTTP_500_INTERNAL_SERVER_ERROR, AppError.code, AppError.message)


async def _app_error_handler(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, AppError)
    if exc.status_code >= 500:
        logger.error("app error", extra={"code": exc.code, "error": exc.message})
    return error_response(exc.status_code, exc.code, exc.message, exc.details, exc.headers)


async def _validation_error_handler(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    details = [{"loc": err["loc"], "message": err["msg"], "type": err["type"]} for err in exc.errors()]
    return error_response(status.HTTP_422_UNPROCESSABLE_CONTENT, "validation_error", "Invalid request", details)


async def _http_error_handler(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)
    # Keeps headers Starlette attaches, e.g. `Allow` on a 405.
    return error_response(exc.status_code, "http_error", str(exc.detail), headers=exc.headers)


async def _unhandled_error_handler(_: Request, exc: Exception) -> JSONResponse:
    # A fallback: `UnhandledErrorMiddleware` normally catches these first, inside the other middleware.
    return internal_error_response(exc)


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, _app_error_handler)
    app.add_exception_handler(RequestValidationError, _validation_error_handler)
    app.add_exception_handler(StarletteHTTPException, _http_error_handler)
    app.add_exception_handler(Exception, _unhandled_error_handler)
