from app.agents.ai_edit.state import EditState, Turn, result_of
from app.constants.ai_edit import AiEditOutcome
from app.core.database import utc_now


async def finish(state: EditState) -> EditState:
    """Adds the request to the chat, and drops what the next run doesn't need from the checkpoint. Rejected
    requests aren't added, like the SPA's history before: they changed nothing and said nothing useful."""
    result = result_of(state)
    if result["outcome"] == AiEditOutcome.REJECTED:
        return {"messages": [], "reply": ""}
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
        commit_sha=None,
        created_at=utc_now().isoformat(),
    )
    return {"turns": [turn], "messages": [], "reply": ""}
