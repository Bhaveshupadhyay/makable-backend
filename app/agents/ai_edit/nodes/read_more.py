from langgraph.runtime import Runtime

from app.agents.ai_edit.state import ContextRef, EditRun, EditState
from app.agents.ai_edit.tools.answer import parse_answer
from app.agents.ai_edit.tools.context import requested_context
from app.schemas.ai_edit import ModelNeedsContext


async def read_more(state: EditState, runtime: Runtime[EditRun]) -> EditState:
    """Answers the model's `needContext`: the lines it asked for go back to it (as a reference, read again when the
    prompt is built), with a note on what it got."""
    answer = parse_answer(state["reply"])
    if not isinstance(answer, ModelNeedsContext):
        raise RuntimeError("read_more needs a needContext answer")
    already: list[ContextRef] = [ref for m in state["messages"] if "context" in m for ref in m["context"]]
    refs, note = requested_context(await runtime.context.files(), answer.need_context, already)
    messages = [
        *state["messages"],
        {"role": "assistant", "content": state["reply"]},
        {"role": "user", "context": refs, "note": f"{note} Now answer with edits, or escalate."},
    ]
    return {"messages": messages, "context_rounds": state["context_rounds"] + 1}
