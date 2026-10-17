from langgraph.runtime import Runtime

from app.agents.ai_edit.prompts.system import SYSTEM_PROMPT
from app.agents.ai_edit.prompts.user import build_user_prompt, earlier_requests, render_ref
from app.agents.ai_edit.state import EditRun, EditState
from app.schemas.chat import ChatMessage, ImagePart, ImageUrl, TextPart


async def generate(state: EditState, runtime: Runtime[EditRun]) -> EditState:
    """Asks the model: the first prompt (with the picture of the selected element, for models that read images),
    then what followed it (its replies, repair reports, code it asked for)."""
    run = runtime.context
    model = await run.model_client()
    files = await run.files()
    prompt = build_user_prompt(
        run.request, state["selection"], files, state["context"], earlier_requests(state.get("turns", []))
    )
    # One user turn, no system role: the free models behind OmniRoute ignore a system message and answer in their
    # own agent format, but follow the same text in the user turn.
    text = f"{SYSTEM_PROMPT}\n\n{prompt}"
    first = ChatMessage(role="user", content=text)
    if run.request.screenshot and model.accepts_images:
        image = ImagePart(image_url=ImageUrl(url=run.request.screenshot))
        first = ChatMessage(role="user", content=[TextPart(text=text), image])
    messages = [first]
    for message in state["messages"]:
        if "context" in message:
            code = "\n\n".join(render_ref(files, ref) for ref in message["context"])
            content = f"{message['note']}\n\n{code}" if code else message["note"]
            messages.append(ChatMessage(role="user", content=content))
        else:
            messages.append(ChatMessage(role=message["role"], content=message["content"]))
    run.model_input = "\n\n".join(m.text for m in messages)
    reply = await model.complete(messages)
    return {"reply": reply, "attempt": state["attempt"] + 1, "model": model.name}
