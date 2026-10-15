from typing import Any

from langgraph.config import get_stream_writer


def progress(stage: str, message: str, **extra: Any) -> None:
    """Tells the browser what the creation is doing (a `custom` stream event; nothing when the run isn't streamed)."""
    get_stream_writer()({"type": "progress", "stage": stage, "message": message, **extra})
