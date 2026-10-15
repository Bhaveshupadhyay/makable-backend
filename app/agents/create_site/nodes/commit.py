import asyncio

from langgraph.runtime import Runtime

from app.agents.create_site.nodes.progress import progress
from app.agents.create_site.state import CreateRun, CreateState
from app.agents.thread import RunResult
from app.constants.ai_edit import AiEditOutcome

LEFT_PROBLEMS = " A few things may still need fixing: if the preview shows an error, ask me to fix it."


async def commit(state: CreateState, runtime: Runtime[CreateRun]) -> CreateState:
    """Commits the written files to the site's repo as one commit on the base commit. Once started, it's finished even
    if the run is cancelled (the service waits for `commit_task`)."""
    run = runtime.context
    progress("commit", "Saving the site to your GitHub")
    message = f"Create {state['name']} from your prompt"
    changes: dict[str, str | None] = dict(run.files)
    run.commit_task = asyncio.ensure_future(run.site.commit(state["base_commit"], changes, message))
    commit_sha = await asyncio.shield(run.commit_task)
    summary = state["summary"] + (LEFT_PROBLEMS if state["problems"] else "")
    result = RunResult(outcome=AiEditOutcome.APPLIED.value, summary=summary, commit_sha=commit_sha)
    return {"result": result, "step": "finish"}
