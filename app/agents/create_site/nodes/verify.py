from langgraph.runtime import Runtime

from app.agents.create_site.nodes.progress import progress
from app.agents.create_site.state import CreateRun, CreateState
from app.agents.create_site.tools.checks import check_site
from app.constants.ai_create import MAX_FIX_ROUNDS


async def verify(state: CreateState, runtime: Runtime[CreateRun]) -> CreateState:
    """Checks the written files together (imports, exports, packages, icons, syntax, and that the page shows the site's
    name and has it as its title). Problems go to `fix` a couple of times; what's left after that is committed anyway
    (the preview shows it, and an AI edit can fix it)."""
    said = progress("check", "Checking the code")
    files = await runtime.context.site_files(state["base_commit"])
    problems = check_site(files, state["written"], state["name"])
    if problems and state["fix_rounds"] < MAX_FIX_ROUNDS:
        return {"problems": problems, "steps": said, "step": "fix"}
    return {"problems": problems, "steps": said, "step": "commit"}
