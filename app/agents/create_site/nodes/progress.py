from typing import Any

from langgraph.config import get_stream_writer


def progress(stage: str, message: str, **extra: Any) -> list[str]:
    """Tells the browser what the creation is doing (a `custom` stream event; nothing when the run isn't streamed).
    Returns the step for the node's `steps` update, so the chat keeps it after the stream ends."""
    get_stream_writer()({"type": "progress", "stage": stage, "message": message, **extra})
    return [message]
