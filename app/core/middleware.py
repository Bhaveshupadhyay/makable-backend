"""ASGI middleware: request ids and access logs, security headers, CORS, body size limits, and the 500 for uncaught
errors."""

import logging
import re
from time import perf_counter
from uuid import uuid4

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.constants.api import MAX_BODY_BYTES, REQUEST_ID_HEADER, ROUTE_BODY_LIMITS
from app.core.config import Settings
from app.core.exceptions import error_response, internal_error_response
from app.core.logging import request_id_var

logger = logging.getLogger("app.access")

_VALID_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


class RequestContextMiddleware:
    """Gives every request an id (reusing a sane incoming `X-Request-ID`), echoes it in the response
    and logs one line per request with its status and duration."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming = Headers(scope=scope).get(REQUEST_ID_HEADER)
        request_id = incoming if incoming and _VALID_REQUEST_ID.match(incoming) else uuid4().hex
        # Not reset afterwards: the error handler for unhandled exceptions runs outside this
        # middleware and still needs it. Each request runs in its own context, so nothing leaks.
        request_id_var.set(request_id)
        status_code = 500
        start = perf_counter()

        async def send_with_request_id(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                MutableHeaders(scope=message).append(REQUEST_ID_HEADER, request_id)
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        finally:
            logger.info(
                "request",
                extra={
                    "method": scope["method"],
                    "path": scope["path"],
                    "status": status_code,
                    "duration_ms": round((perf_counter() - start) * 1000, 2),
                },
            )


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp, *, hsts: bool) -> None:
        self.app = app
        self.headers = {
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
            "Referrer-Policy": "strict-origin-when-cross-origin",
            "Cross-Origin-Opener-Policy": "same-origin",
        }
        if hsts:
            self.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for key, value in self.headers.items():
                    headers.setdefault(key, value)
            await send(message)

        await self.app(scope, receive, send_with_headers)


class BodySizeLimitMiddleware:
    """Reads request bodies up to a limit (per route, see `ROUTE_BODY_LIMITS`) and answers 413 past it, before
    the app parses anything, so an oversized request can't use more memory than the limit. Declared sizes
    (`Content-Length`) are refused without reading the body; streamed ones as soon as they pass the limit.

    The body is read here and handed on in one piece, rather than failing from the app's `receive`: FastAPI
    turns any error while reading the body into a generic 400.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        default: int = MAX_BODY_BYTES,
        routes: tuple[tuple[str, str, int], ...] = ROUTE_BODY_LIMITS,
    ) -> None:
        self.app = app
        self.default = default
        self.routes = routes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] in ("GET", "HEAD", "OPTIONS"):
            await self.app(scope, receive, send)
            return

        limit = self._limit(scope["method"], scope["path"])
        declared = Headers(scope=scope).get("content-length")
        if declared is not None and declared.isdigit() and int(declared) > limit:
            await _too_large(limit)(scope, receive, send)
            return

        chunks: list[bytes] = []
        size = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            size += len(chunk)
            if size > limit:
                await _too_large(limit)(scope, receive, send)
                return
            chunks.append(chunk)
            if not message.get("more_body", False):
                break

        body = b"".join(chunks)
        delivered = False

        async def replay() -> Message:
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": body, "more_body": False}
            # Later reads wait for the client to go away, as usual (disconnect detection relies on it).
            return await receive()

        await self.app(scope, replay, send)

    def _limit(self, method: str, path: str) -> int:
        for route_method, prefix, size in self.routes:
            if method == route_method and path.startswith(prefix):
                return size
        return self.default


def _too_large(limit: int) -> ASGIApp:
    return error_response(413, "request_too_large", f"The request is too large (at most {limit} bytes).")


class UnhandledErrorMiddleware:
    """Turns an uncaught exception into the generic 500 error envelope. Starlette's own handler for
    `Exception` runs outside every other middleware, so its 500 would miss the request id, security and
    CORS headers. This one is registered innermost, so its response passes through all of them."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        started = False

        async def send_tracking_start(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, receive, send_tracking_start)
        except Exception as exc:
            if started:  # Too late for an error response; let the server close the connection.
                raise
            await internal_error_response(exc)(scope, receive, send)


def register_middleware(app: FastAPI, settings: Settings) -> None:
    """Registers middleware. The last one added runs first, so request context wraps everything and
    uncaught errors are turned into responses inside all of it."""
    app.add_middleware(UnhandledErrorMiddleware)
    app.add_middleware(BodySizeLimitMiddleware)
    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
            expose_headers=[REQUEST_ID_HEADER],
        )
    app.add_middleware(SecurityHeadersMiddleware, hsts=settings.secure_cookies)
    app.add_middleware(RequestContextMiddleware)
