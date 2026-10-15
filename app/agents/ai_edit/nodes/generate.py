from langgraph.runtime import Runtime

from app.agents.ai_edit.prompts.system import SYSTEM_PROMPT
from app.agents.ai_edit.prompts.user import build_user_prompt, earlier_requests
from app.agents.ai_edit.state import EditRun, EditState
from app.schemas.chat import ChatMessage


async def generate(state: EditState, runtime: Runtime[EditRun]) -> EditState:
    """Asks the model for edits: the first prompt, then any rejected replies and their repair prompts."""
    run = runtime.context
    model = await run.model_client()
    if not run.user_prompt:
        run.user_prompt = build_user_prompt(run.request, earlier_requests(state.get("turns", [])))
    # One user turn, no system role: the free models behind OmniRoute ignore a system message and answer in their
    # own agent format, but follow the same text in the user turn.
    messages = [
        ChatMessage(role="user", content=f"{SYSTEM_PROMPT}\n\n{run.user_prompt}"),
        *(ChatMessage.model_validate(m) for m in state["messages"]),
    ]
    reply = await model.complete(messages)
    return {"reply": reply, "attempt": state["attempt"] + 1}
