from langgraph.types import interrupt

from app.agents.create_site.state import CreateState
from app.agents.create_site.tools.names import suggest_name
from app.agents.thread import QuestionAnswer
from app.constants.site import SITE_NAME_MAX

NAME_QUESTION = "What should we call this site? It's also the name of its repository on your GitHub."


async def ask_name(state: CreateState) -> CreateState:
    """Asks the user for the site's name (with one suggested from their prompt), pausing the run: nothing is written
    before the user names the site. A site that has its name and repo already skips this. Everything before
    `interrupt` runs again on resume, so it only reads the state."""
    if state["name"]:
        return {"step": "setup"}
    problem = state["name_problem"]
    question = f"{problem} What should we call this site instead?" if problem else NAME_QUESTION
    options = [] if problem else [suggest_name(state["prompt"])]
    reply = interrupt(
        {"type": "question", "kind": "project_name", "question": question, "options": options, "allowFreeText": True}
    )
    name = " ".join(str(reply).split())[:SITE_NAME_MAX]
    asked = QuestionAnswer(question=question, answer=name)
    return {"name": name, "name_problem": "", "questions": [*state["questions"], asked], "step": "setup"}
