from langgraph.runtime import Runtime
from langgraph.types import interrupt

from app.agents.ai_edit.prompts.repair import preview_prompt
from app.agents.ai_edit.state import EditRun, EditState, RunResult, result_of
from app.constants.ai_edit import MAX_PREVIEW_ROUNDS, AiEditOutcome
from app.schemas.ai_edit import AiEditPreviewReport, FileEdit
from app.utils.file_edits import apply_file_edits


async def preview(state: EditState, runtime: Runtime[EditRun]) -> EditState:
    """Pauses the run (`interrupt`) until the SPA has tried the edits in its preview: nothing is committed before the
    preview works. The service answers with the files the edits make; the SPA reports back (`Command(resume=report)`).
    It works: on to the commit. It failed: back to the model with what the preview showed (≤ `MAX_PREVIEW_ROUNDS`
    times), then the request gives up and the SPA undoes the edits in its preview.

    Everything before `interrupt` runs again on resume, so it only reads the state."""
    result = result_of(state)
    resumed = interrupt({"type": "preview", "summary": result.get("summary", "")})
    report = AiEditPreviewReport.model_validate(resumed)
    if report.ok:
        return {"step": "commit"}
    error = report.error
    message = error.message if error else "the preview failed"
    rounds = state["preview_rounds"] + 1
    if rounds > MAX_PREVIEW_ROUNDS or error is None:
        failed = RunResult(outcome=AiEditOutcome.REJECTED.value, problem=f"The preview failed: {message}"[:500])
        return {"step": "finish", "preview_rounds": rounds, "result": failed}
    edits = [FileEdit.model_validate(e) for e in result.get("edits", [])]
    edited = apply_file_edits(await runtime.context.files(), edits).files
    messages = [
        *state["messages"],
        {"role": "assistant", "content": state["reply"]},
        {"role": "user", "content": preview_prompt(error, edited)},
    ]
    return {"step": "generate", "preview_rounds": rounds, "messages": messages, "result": None}
