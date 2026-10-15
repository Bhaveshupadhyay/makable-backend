from langgraph.runtime import Runtime

from app.agents.ai_edit.state import EditRun, EditState
from app.agents.ai_edit.tools.context import first_context, locate_selection


async def load_context(state: EditState, runtime: Runtime[EditRun]) -> EditState:
    """Reads the site at the request's base commit, finds the selected element in the code, and picks the code the
    model starts with."""
    files = await runtime.context.files()
    selection = locate_selection(files, runtime.context.request.target)
    return {"selection": selection, "context": first_context(files, selection)}
