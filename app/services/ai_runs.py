"""What the AI services share about running agents: each agent's thread for a site, how a run is saved, what a paused
run waits for, and ending one that's paused."""

import logging
from typing import Any
from uuid import UUID

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.types import Durability, StateSnapshot

from app.agents.thread import RECURSION_LIMIT, RunResult
from app.agents.usage import Usage
from app.constants.ai_create import CREATE_THREAD_SUFFIX
from app.constants.ai_edit import AiEditOutcome
from app.repositories.site import AiEditRecordRepository
from app.repositories.unit_of_work import UnitOfWork
from app.schemas.ai_edit import AiUsage
from app.schemas.site import AiEditRecordCreate

logger = logging.getLogger(__name__)

# The checkpoint is written when a run ends (or pauses), not after every step: a run is short, and a cancelled or
# failed one then leaves nothing half-done behind (only its input), which the next run's input replaces.
DURABILITY: Durability = "exit"


def edit_config(project_id: UUID) -> Any:
    """The edit agent's thread for a site (its id is the `projectId`)."""
    return {"configurable": {"thread_id": str(project_id)}, "recursion_limit": RECURSION_LIMIT}


def create_config(project_id: UUID) -> Any:
    """The create agent's thread for a site."""
    return {"configurable": {"thread_id": f"{project_id}{CREATE_THREAD_SUFFIX}"}, "recursion_limit": RECURSION_LIMIT}


def pause_value(snapshot: StateSnapshot) -> dict[str, Any] | None:
    """What a paused run is waiting on (its `interrupt` value: a question or a preview), if the thread has one."""
    for interrupt in snapshot.interrupts:
        if isinstance(interrupt.value, dict) and interrupt.value.get("type") in ("question", "preview"):
            return interrupt.value
    return None


def pause_kind(snapshot: StateSnapshot) -> str | None:
    """ "question" or "preview" when a run is waiting for one, else None."""
    value = pause_value(snapshot)
    return value["type"] if value else None


async def end_paused(
    graph: Any,
    config: Any,
    snapshot: StateSnapshot,
    *,
    records: AiEditRecordRepository,
    uow: UnitOfWork,
    user_id: UUID,
    project_id: UUID,
) -> bool:
    """Ends a paused run (a new request or Dismiss replaces it): the thread moves past it as if it finished, and it's
    recorded as skipped. Returns whether there was one."""
    if pause_kind(snapshot) is None:
        return False
    values = snapshot.values
    result = RunResult(outcome=AiEditOutcome.SKIPPED.value)
    await graph.aupdate_state(config, {"result": result}, as_node="finish")
    request = values.get("request") or {}
    await records.add(
        AiEditRecordCreate(
            site_id=project_id,
            user_id=user_id,
            instruction=values.get("instruction") or values.get("prompt") or "",
            selection=request.get("target"),
            model=values.get("model") or None,
            tier=None,
            outcome=AiEditOutcome.SKIPPED,
            summary="replaced or dismissed while waiting",
            attempts=values.get("attempt", 0),
            usage=usage_read(values.get("usage")),
        )
    )
    await uow.commit()
    return True


def usage_read(usage: Usage | None) -> AiUsage | None:
    """A run's usage (from its state or its meter) as it's recorded and sent."""
    return AiUsage.model_validate(usage) if usage is not None else None


async def prune(checkpointer: BaseCheckpointSaver[str], config: Any) -> None:
    """Keeps only a thread's latest checkpoint: it has the whole chat, and nothing reads older ones."""
    try:
        await checkpointer.aprune([config["configurable"]["thread_id"]], strategy="keep_latest")
    except NotImplementedError:
        pass  # The in-memory checkpointer (tests, SQLite dev) can't prune; it's gone at exit anyway.
    except Exception:
        # Best effort: the next run prunes again.
        logger.warning("Couldn't prune an AI thread's checkpoints", exc_info=True)
