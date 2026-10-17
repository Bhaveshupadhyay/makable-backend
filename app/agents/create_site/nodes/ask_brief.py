from langgraph.types import interrupt

from app.agents.create_site.state import CreateState
from app.agents.thread import QuestionAnswer, read_reply
from app.constants.ai_edit import MAX_ANSWER


async def ask_brief(state: CreateState) -> CreateState:
    """Asks the user the question about the site `brief` came up with, pausing the run; `brief` then reads the answer,
    or the message about the question, with the ones before it. Nothing is made before the questions and the name.
    Everything before `interrupt` runs again on resume, so it only reads the state."""
    asking = state["brief_question"]
    if not asking:
        return {"step": "ask_name"}
    reply = read_reply(interrupt({"type": "question", **asking}))
    text = reply["answer"].strip()[:MAX_ANSWER]
    asked = QuestionAnswer(question=asking["question"], answer=text, steps=state["steps"])
    if reply["chat"]:
        asked["chat"] = True
    return {"brief": [*state["brief"], asked], "questions": [*state["questions"], asked], "steps": [], "step": "brief"}
