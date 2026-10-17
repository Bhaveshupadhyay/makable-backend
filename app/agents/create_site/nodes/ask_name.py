from langgraph.types import interrupt

from app.agents.create_site.state import CreateState
from app.agents.create_site.tools.names import suggest_name
from app.agents.thread import QuestionAnswer, read_reply
from app.constants.ai_edit import MAX_ANSWER

NAME_QUESTION = "What should we call this site? It's also the name of its repository on your GitHub."


def name_question(state: CreateState) -> tuple[str, list[str]]:
    """The name question the user is asked now, and its options: the first one has the names the model suggested after
    the questions about the site, or one taken from the prompt."""
    if state["name_question"]:
        return state["name_question"], state["name_options"]
    return NAME_QUESTION, state["suggested_names"] or [suggest_name(state["prompt"])]


async def ask_name(state: CreateState) -> CreateState:
    """Asks the user for the site's name, after the questions about the site (`brief`), pausing the run: nothing is
    written before the user names the site. What they answer is read by `read_name` (it may be a question, or not a
    name at all). A site that has its name and repo already skips this. Everything before `interrupt` runs again on
    resume, so it only reads the state."""
    if state["name"]:
        return {"step": "setup"}
    question, options = name_question(state)
    asking = {"type": "question", "kind": "project_name", "question": question, "options": options}
    reply = read_reply(interrupt({**asking, "allowFreeText": True}))
    text = reply["answer"].strip()[:MAX_ANSWER]
    asked = QuestionAnswer(question=question, answer=text, steps=state["steps"])
    if reply["chat"]:
        asked["chat"] = True
    return {
        "name_reply": text,
        "name_chat": reply["chat"],
        "questions": [*state["questions"], asked],
        "steps": [],
        "step": "read_name",
    }
