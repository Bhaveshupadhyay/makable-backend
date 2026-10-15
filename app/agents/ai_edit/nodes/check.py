import logging

from langgraph.runtime import Runtime

from app.agents.ai_edit.prompts.repair import rejection_report, repair_prompt
from app.agents.ai_edit.state import EditRun, EditState
from app.agents.ai_edit.tools.answer import describe_problem, parse_answer, without_line_numbers
from app.agents.ai_edit.tools.edits import check_edits
from app.agents.thread import RunResult
from app.constants.ai_edit import MAX_ATTEMPTS, MAX_CONTEXT_ROUNDS, MAX_QUESTIONS, AiEditOutcome
from app.schemas.ai_edit import ModelEdits, ModelEscalation, ModelNeedsContext, ModelQuestion
from app.utils.file_edits import FileEditError

logger = logging.getLogger(__name__)


async def check(state: EditState, runtime: Runtime[EditRun]) -> EditState:
    """Checks the model's reply and says where the run goes next (`step`): more code for the model, a question for the
    user, the preview for edits that passed the dry run, a repair (a rejected reply goes back once with a report of what
    failed and where, because free models often get the shape or the search text wrong the first time), or the end."""
    reply, attempt = state["reply"], state["attempt"]
    # Model calls that answered (or tried to): the ones that asked for code or asked the user don't count against
    # the retries.
    asked = len(state["questions"])
    answers = attempt - state["context_rounds"] - asked - state["preview_rounds"]
    answer: ModelEdits | ModelEscalation | ModelNeedsContext | ModelQuestion | None = None
    try:
        answer = parse_answer(reply)
        if isinstance(answer, ModelEscalation):
            escalated = RunResult(outcome=AiEditOutcome.ESCALATED.value, tier=2, reason=answer.escalate)
            return {"step": "finish", "result": escalated}
        if isinstance(answer, ModelNeedsContext):
            if state["context_rounds"] < MAX_CONTEXT_ROUNDS:
                return {"step": "read_more"}
            raise ValueError("you can't ask for more code now: answer with edits for what you were shown, or escalate")
        if isinstance(answer, ModelQuestion):
            if asked < MAX_QUESTIONS:
                return {"step": "ask_user"}
            raise ValueError(
                "you can't ask the user anything more: make the change with a sensible choice and say which in the "
                "summary, or escalate"
            )
        edits = without_line_numbers(answer.edits)
        check_edits(await runtime.context.files(), edits)
    except (ValueError, FileEditError) as err:  # ValidationError and JSONDecodeError are ValueErrors.
        problem = describe_problem(err)
        logger.info("AI edit answer rejected", extra={"attempt": attempt, "problem": problem[:300]})
        if answers >= MAX_ATTEMPTS:
            rejected = RunResult(outcome=AiEditOutcome.REJECTED.value, problem=problem)
            return {"step": "finish", "problem": problem, "result": rejected}
        sent = without_line_numbers(answer.edits) if isinstance(answer, ModelEdits) else None
        repair = repair_prompt(rejection_report(await runtime.context.files(), sent, err))
        messages = [*state["messages"], {"role": "assistant", "content": reply}, {"role": "user", "content": repair}]
        return {"step": "generate", "problem": problem, "messages": messages}
    applied = RunResult(
        outcome=AiEditOutcome.APPLIED.value, tier=1, summary=answer.summary, edits=[e.model_dump() for e in edits]
    )
    return {"step": "preview", "result": applied}
