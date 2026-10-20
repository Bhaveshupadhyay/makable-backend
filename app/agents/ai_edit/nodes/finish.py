from langgraph.runtime import Runtime

from app.agents.ai_edit.state import EditRun, EditState, result_of
from app.agents.thread import Turn
from app.agents.usage import Usage, shown_usage
from app.constants.ai_edit import AiEditOutcome
from app.core.database import utc_now


async def finish(state: EditState, runtime: Runtime[EditRun]) -> EditState:
    """Adds the request to the chat, and drops what the next run doesn't need from the checkpoint. Rejected
    requests aren't added: they changed nothing and said nothing useful."""
    run = runtime.context
    usage = run.usage.total()
    result = result_of(state)
    if result["outcome"] == AiEditOutcome.REJECTED:
        return {"messages": [], "reply": "", "usage": usage}
    turn = turn_for(state, shown_usage(usage, run.show_usage))
    return {"turns": [turn], "messages": [], "reply": "", "usage": usage}


def turn_for(state: EditState, usage: Usage | None = None) -> Turn:
    """The chat entry for a run that ended with a result, with the usage the chat shows."""
    result = result_of(state)
    if result["outcome"] == AiEditOutcome.ESCALATED:
        reply = f"Not done: it needs a deeper edit ({result.get('reason', '')})."
    else:
        reply = result.get("summary", "")
    turn = Turn(
        run_id=state["run_id"],
        instruction=state["instruction"],
        target=state["target"],
        outcome=result["outcome"],
        reply=reply,
        questions=state.get("questions", []),
        commit_sha=result.get("commit_sha"),
        created_at=utc_now().isoformat(),
    )
    if usage is not None:
        turn["usage"] = usage
    return turn
