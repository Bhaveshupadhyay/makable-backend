"""Stops slow work when the browser goes away. Starlette keeps running a handler after the client disconnects,
so a model call would run to the end (and be paid for) after the user pressed Stop."""

import asyncio
from collections.abc import Coroutine
from typing import Any

from fastapi import Request

from app.core.exceptions import AppError

POLL_SECONDS = 0.5
# How long a cancelled piece of work gets to clean up (e.g. record that it was stopped) before the request ends and
# its database session closes.
CLEANUP_SECONDS = 5


class ClientDisconnectedError(AppError):
    # nginx's "client closed request". Nobody reads the response; the status is for the logs.
    status_code = 499
    code = "client_closed_request"
    message = "The client went away"


async def cancel_on_disconnect[T](request: Request, work: Coroutine[Any, Any, T]) -> T:
    """Runs `work`, and cancels it if the client disconnects first.

    Raises:
        ClientDisconnectedError: The client disconnected before `work` finished.
    """
    task = asyncio.ensure_future(work)
    try:
        while True:
            done, _ = await asyncio.wait({task}, timeout=POLL_SECONDS)
            if done:
                return task.result()
            if await request.is_disconnected():
                raise ClientDisconnectedError()
    finally:
        if not task.done():
            task.cancel()
            await asyncio.wait({task}, timeout=CLEANUP_SECONDS)
