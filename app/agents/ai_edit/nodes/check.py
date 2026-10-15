import logging

from langgraph.runtime import Runtime

from app.agents.ai_edit.prompts.repair import rejection_report, repair_prompt
from app.agents.ai_edit.state import EditRun, EditState
from app.agents.ai_edit.tools.answer import describe_problem, parse_answer
from app.agents.ai_edit.tools.edits import check_edits
from app.constants.ai_edit import MAX_ATTEMPTS, AiEditOutcome
from app.schemas.ai_edit import ModelEdits, ModelEscalation
from app.utils.file_edits import FileEditError

logger = logging.getLogger(__name__)


async def check(state: EditState, runtime: Runtime[EditRun]) -> EditState:
    """Checks the model's reply. A rejected one goes back to the model once with a repair prompt and a report of
    what failed and where (the file's real text there), because free models often get the shape or the search text
    wrong the first time."""
    request, reply, attempt = runtime.context.request, state["reply"], state["attempt"]
    answer: ModelEdits | ModelEscalation | None = None
    try:
        answer = parse_answer(reply)
        if isinstance(answer, ModelEscalation):
            return {"result": {"outcome": AiEditOutcome.ESCALATED.value, "tier": 2, "reason": answer.escalate}}
        check_edits(request, answer.edits)
    except (ValueError, FileEditError) as err:
        problem = describe_problem(err)
        logger.info("AI edit answer rejected", extra={"attempt": attempt, "problem": problem[:300]})
        if attempt >= MAX_ATTEMPTS:
            return {"problem": problem, "result": {"outcome": AiEditOutcome.REJECTED.value, "problem": problem}}
        sent = answer.edits if isinstance(answer, ModelEdits) else None
        repair = repair_prompt(rejection_report(request, sent, err))
        return {
            "problem": problem,
            "messages": [
                *state["messages"],
                {"role": "assistant", "content": reply},
                {"role": "user", "content": repair},
            ],
        }
    edits = [e.model_dump() for e in answer.edits]
    return {"result": {"outcome": AiEditOutcome.APPLIED.value, "tier": 1, "summary": answer.summary, "edits": edits}}
