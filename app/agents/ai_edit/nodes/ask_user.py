from langgraph.runtime import Runtime
from langgraph.types import interrupt

from app.agents.ai_edit.state import EditRun, EditState
from app.agents.ai_edit.tools.answer import parse_answer
from app.agents.thread import QuestionAnswer, read_reply
from app.constants.ai_edit import MAX_ANSWER
from app.schemas.ai_edit import ModelQuestion


async def ask_user(state: EditState, runtime: Runtime[EditRun]) -> EditState:
    """Pauses the run on the model's question (`interrupt`): the run is saved, and the service answers the request with
    the question. The user's answer resumes it here (`Command(resume=answer)`), maybe much later, from another tab, and
    on a newer commit of the site (the code is read again there). A message about the question ("Chat about this")
    goes to the model as one, and it replies by asking again; asking again then doesn't count against `MAX_QUESTIONS`.

    Everything before `interrupt` runs again on resume, so it only reads the state."""
    answer = parse_answer(state["reply"])
    if not isinstance(answer, ModelQuestion):
        raise RuntimeError("ask_user needs an ask answer")
    question = answer.ask
    reply = read_reply(interrupt({"type": "question", **question.model_dump(mode="json", by_alias=True)}))
    text = reply["answer"][:MAX_ANSWER]
    if reply["chat"]:
        content = (
            "The user didn't answer your question yet: they chose to talk about it, and wrote:\n"
            f"<message>\n{text}\n</message>\n"
            "If it tells you what to do, answer with edits. Otherwise ask again: an ask answer whose question first "
            "replies to what they wrote in a sentence or two (in their language), then asks your question, with the "
            "options that fit now. The code below is the site as it is now."
        )
    else:
        content = (
            f"The user answered your question:\n<answer>\n{text}\n</answer>\n"
            "The code below is the site as it is now. Answer with edits, or escalate."
        )
    messages = [
        *state["messages"],
        {"role": "assistant", "content": state["reply"]},
        {"role": "user", "content": content},
    ]
    asked = QuestionAnswer(question=question.question, answer=text)
    if reply["chat"]:
        asked["chat"] = True
    return {"messages": messages, "questions": [*state["questions"], asked]}
