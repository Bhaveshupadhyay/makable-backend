from langgraph.runtime import Runtime

from app.agents.ai_edit.tools.answer import describe_problem
from app.agents.create_site.nodes.progress import progress
from app.agents.create_site.prompts.build import plan_prompt
from app.agents.create_site.prompts.system import CREATE_SYSTEM
from app.agents.create_site.state import CreateFailedError, CreateRun, CreateState, PlannedFile
from app.agents.create_site.tools.plan import SitePlan, parse_plan
from app.constants.ai_create import MAX_PLAN_ATTEMPTS
from app.schemas.chat import ChatMessage


async def plan(state: CreateState, runtime: Runtime[CreateRun]) -> CreateState:
    """Asks the model which files the site needs (a rejected plan goes back once with what was wrong). The stylesheet
    is written first and App.tsx last, so every file can see what it imports.

    Raises:
        CreateFailedError: No valid plan in the tries.
    """
    run = runtime.context
    model = await run.model_client()
    progress("plan", "Planning the site")
    text = f"{CREATE_SYSTEM}\n\n{plan_prompt(state['prompt'], state['name'])}"
    run.model_input = text
    messages = [ChatMessage(role="user", content=text)]
    attempt = state["attempt"]
    planned: SitePlan | None = None
    for _ in range(MAX_PLAN_ATTEMPTS):
        reply = await model.complete(messages)
        attempt += 1
        try:
            planned = parse_plan(reply)
            break
        except ValueError as err:
            problem = describe_problem(err)
            messages += [
                ChatMessage(role="assistant", content=reply),
                ChatMessage(role="user", content=f"That plan was rejected: {problem}. Reply with the corrected JSON."),
            ]
    if planned is None:
        raise CreateFailedError("the model couldn't plan the site")
    files = sorted(planned.files, key=lambda f: (f.path != "src/index.css", f.path == "src/App.tsx"))
    plan_files = [PlannedFile(path=f.path, purpose=f.purpose) for f in files]
    return {
        "plan": plan_files,
        "summary": planned.summary,
        "index": 0,
        "written": [],
        "attempt": attempt,
        "model": model.name,
        "step": "write",
    }
