from langgraph.runtime import Runtime

from app.agents.ai_edit.tools.answer import describe_problem
from app.agents.create_site.nodes.ask_name import NAME_QUESTION, name_question
from app.agents.create_site.nodes.progress import progress
from app.agents.create_site.prompts.name import name_prompt
from app.agents.create_site.state import CreateRun, CreateState
from app.agents.create_site.tools.names import NameChat, NameGiven, mentions, parse_name_reply, same_name
from app.constants.ai_create import MAX_NAME_READ_ATTEMPTS
from app.constants.ai_edit import MAX_OPTION
from app.constants.site import SITE_NAME_MAX
from app.schemas.chat import ChatMessage


async def read_name(state: CreateState, runtime: Runtime[CreateRun]) -> CreateState:
    """Reads the user's answer to the name question. A clicked option is the name; anything else goes to the model,
    which finds the name in it ("call it Sunrise") or, when it isn't one (a greeting, a question about names, a
    request for ideas), answers it and asks again, with names to suggest. A message about the question ("Chat about
    this") is always answered, never taken as the name. The repo is only made with a name the user wrote or was shown
    as an answer: one the model came up with, or one from a chat, is asked about first."""
    reply, chat = state["name_reply"], state["name_chat"]
    question, options = name_question(state)
    picked = None if chat else next((o for o in options if same_name(o, reply)), None)
    if picked is not None:
        return _named(picked, {"attempt": state["attempt"]})
    run = runtime.context
    model = await run.model_client()
    said = progress("name", "Reading your answer")
    asked = name_prompt(state["prompt"], state["brief"], question, options, reply, chat=chat)
    messages = [ChatMessage(role="user", content=asked)]
    attempt = state["attempt"]
    read: NameGiven | NameChat | None = None
    for _ in range(MAX_NAME_READ_ATTEMPTS):
        text = (await model.complete(messages)).text
        attempt += 1
        try:
            read = parse_name_reply(text)
            break
        except ValueError as err:
            messages += [
                ChatMessage(role="assistant", content=text),
                ChatMessage(
                    role="user",
                    content=f"That reply was rejected: {describe_problem(err)}. Reply with the corrected JSON.",
                ),
            ]
    used: CreateState = {"attempt": attempt, "usage": run.usage.total()}
    if read is None:
        return _ask_again(f"Sorry, I didn't catch a name there. {NAME_QUESTION}", options, used, said)
    if isinstance(read, NameChat):
        return _ask_again(read.reply, read.suggestions, used, said)
    name = " ".join(read.name.split())[:SITE_NAME_MAX]
    if not chat and (mentions(reply, name) or any(same_name(o, name) for o in options)):
        return {**_named(name, used), "steps": said}
    return _ask_again(f'Should we call it "{name}"?', [name] if len(name) <= MAX_OPTION else [], used, said)


def _named(name: str, used: CreateState) -> CreateState:
    """`used`: the model calls made (`attempt`), and the run's usage when the model was called."""
    return {"name": name, "name_question": "", "name_options": [], **used, "step": "setup"}


def _ask_again(question: str, options: list[str], used: CreateState, said: list[str]) -> CreateState:
    return {"name_question": question, "name_options": options, **used, "steps": said, "step": "ask_name"}
