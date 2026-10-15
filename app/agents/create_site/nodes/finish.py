from app.agents.create_site.state import CreateState
from app.agents.thread import Turn
from app.core.database import utc_now


async def finish(state: CreateState) -> CreateState:
    """Adds the creation to the chat."""
    result = state["result"]
    if result is None:
        raise RuntimeError("the creation ended without a result")
    turn = Turn(
        run_id=state["run_id"],
        instruction=state["prompt"],
        target=None,
        outcome=result["outcome"],
        reply=result.get("summary", ""),
        questions=state["questions"],
        commit_sha=result.get("commit_sha"),
        created_at=utc_now().isoformat(),
    )
    return {"turns": [turn]}
