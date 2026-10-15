import asyncio

from langgraph.runtime import Runtime

from app.agents.ai_edit.state import EditRun, EditState, result_of
from app.schemas.ai_edit import FileEdit
from app.utils.file_edits import apply_file_edits


async def commit(state: EditState, runtime: Runtime[EditRun]) -> EditState:
    """Commits the checked edits to the site's repo, on top of the request's base commit, with the instruction as the
    commit message. The last step of a run, and nothing slow comes after it.

    Once started, the commit is finished even if the run is cancelled (Stop): cutting it short could move the branch
    without the browser knowing. The service waits for `commit_task` when a run is cancelled, and records it.

    Raises:
        SiteConflictError: The site isn't at the base commit on GitHub any more (another tab or device saved).
    """
    run = runtime.context
    result = result_of(state)
    edits = [FileEdit.model_validate(e) for e in result.get("edits", [])]
    files = await run.files()
    applied = apply_file_edits(files, edits)
    changes: dict[str, str | None] = {path: applied.files[path] for path in applied.changed}
    message = state["instruction"].splitlines()[0][:200]
    run.changed_files = changes
    run.committing = result
    run.commit_task = asyncio.ensure_future(run.site.commit(state["base_commit"], changes, message))
    commit_sha = await asyncio.shield(run.commit_task)
    return {"result": {**result, "commit_sha": commit_sha}}
