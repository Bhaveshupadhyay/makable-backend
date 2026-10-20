from langgraph.runtime import Runtime

from app.agents.create_site.state import CreateRun, CreateState
from app.agents.thread import Turn
from app.agents.usage import shown_usage
from app.core.database import utc_now


async def finish(state: CreateState, runtime: Runtime[CreateRun]) -> CreateState:
    """Adds the creation to the chat, with what it took when the chat shows that."""
    run = runtime.context
    usage = run.usage.total()
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
        steps=state["steps"],
        commit_sha=result.get("commit_sha"),
        created_at=utc_now().isoformat(),
    )
    if (shown := shown_usage(usage, run.show_usage)) is not None:
        turn["usage"] = shown
    return {"turns": [turn], "usage": usage}
