from app.agents.ai_edit.state import EditState, result_of
from app.agents.thread import Turn
from app.constants.ai_edit import AiEditOutcome
from app.core.database import utc_now


async def finish(state: EditState) -> EditState:
    """Adds the request to the chat, and drops what the next run doesn't need from the checkpoint. Rejected
    requests aren't added: they changed nothing and said nothing useful."""
    result = result_of(state)
    if result["outcome"] == AiEditOutcome.REJECTED:
        return {"messages": [], "reply": ""}
    return {"turns": [turn_for(state)], "messages": [], "reply": ""}


def turn_for(state: EditState) -> Turn:
    """The chat entry for a run that ended with a result."""
    result = result_of(state)
    if result["outcome"] == AiEditOutcome.ESCALATED:
        reply = f"Not done: it needs a deeper edit ({result.get('reason', '')})."
    else:
        reply = result.get("summary", "")
    return Turn(
        run_id=state["run_id"],
        instruction=state["instruction"],
        target=state["target"],
        outcome=result["outcome"],
        reply=reply,
        questions=state.get("questions", []),
        commit_sha=result.get("commit_sha"),
        created_at=utc_now().isoformat(),
    )
