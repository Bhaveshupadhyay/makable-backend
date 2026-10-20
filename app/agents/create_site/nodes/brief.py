from langgraph.runtime import Runtime

from app.agents.ai_edit.tools.answer import describe_problem
from app.agents.create_site.nodes.progress import progress
from app.agents.create_site.prompts.brief import brief_prompt
from app.agents.create_site.state import CreateRun, CreateState
from app.agents.create_site.tools.brief import answered, parse_brief_reply
from app.constants.ai_create import MAX_BRIEF_ATTEMPTS, MAX_BRIEF_QUESTIONS
from app.schemas.ai_edit import ModelQuestion
from app.schemas.chat import ChatMessage


async def brief(state: CreateState, runtime: Runtime[CreateRun]) -> CreateState:
    """The run's first step: asks the model what to ask the user about the site before anything else, what kind of
    site it is (unless the description says), then what the plan needs that the description doesn't say, one
    question at a time (`ask_brief` asks it), ≤ `MAX_BRIEF_QUESTIONS`. When it's ready, it suggests names for the site,
    the options of the name question that comes next. A message about a question ("Chat about this") is replied to in
    the question asked again. A model that can't reply in its tries doesn't stop the creation: the name is asked with
    one taken from the prompt."""
    done = answered(state["brief"])
    if done >= MAX_BRIEF_QUESTIONS and state["name"]:
        # Nothing more to ask, and no names to suggest.
        return {"brief_question": None, "step": "ask_name"}
    run = runtime.context
    model = await run.model_client()
    said = progress("brief", "Thinking about your site")
    text = brief_prompt(state["prompt"], state["name"], state["brief"], done)
    messages = [ChatMessage(role="user", content=text)]
    attempt = state["attempt"]

    def used() -> CreateState:
        return {"attempt": attempt, "model": model.name, "usage": run.usage.total()}

    for _ in range(MAX_BRIEF_ATTEMPTS):
        reply = (await model.complete(messages)).text
        attempt += 1
        try:
            read = parse_brief_reply(reply)
        except ValueError as err:
            messages += [
                ChatMessage(role="assistant", content=reply),
                ChatMessage(
                    role="user",
                    content=f"That reply was rejected: {describe_problem(err)}. Reply with the corrected JSON.",
                ),
            ]
            continue
        if not isinstance(read, ModelQuestion):
            names = list(dict.fromkeys(read.names))
            ready: CreateState = {"brief_question": None, "suggested_names": names, "step": "ask_name"}
            return {**ready, **used(), "steps": said}
        if done < MAX_BRIEF_QUESTIONS:
            question = read.ask.model_copy(update={"allow_free_text": True, "kind": "site_brief"})
            asking = question.model_dump(mode="json", by_alias=True)
            return {"brief_question": asking, **used(), "steps": said, "step": "ask_brief"}
        break  # A question past the limit: the questions are over anyway.
    else:
        if state["brief"] and state["brief"][-1].get("chat") and state["brief_question"]:
            # The user's message is left unanswered: the question it was about is asked again as it was.
            return {**used(), "steps": said, "step": "ask_brief"}
    return {"brief_question": None, **used(), "steps": said, "step": "ask_name"}
