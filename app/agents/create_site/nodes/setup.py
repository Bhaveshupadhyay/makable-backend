from langgraph.runtime import Runtime

from app.agents.create_site.nodes.progress import progress
from app.agents.create_site.state import CreateRun, CreateState, NameTakenError
from app.constants.ai_create import MAX_NAME_TRIES


async def setup(state: CreateState, runtime: Runtime[CreateRun]) -> CreateState:
    """Creates the site's private repo on the user's GitHub, named after the site, with the starter files: what the
    files are written on. A name whose repo is taken goes back to the user (a few times, then the run fails).

    Raises:
        NameTakenError: The names the user gave were all taken.
    """
    if state["base_commit"]:
        return {"step": "plan"}
    said = progress("setup", f"Creating the repository for {state['name']} on your GitHub")
    try:
        base = await runtime.context.site.create(state["name"])
    except NameTakenError as err:
        tries = state["name_tries"] + 1
        if tries >= MAX_NAME_TRIES:
            raise
        question = (
            f'You already have a repository called "{err.repo_name}" on GitHub. What should we call this site instead?'
        )
        asking: CreateState = {"name": "", "name_question": question, "name_options": [], "name_tries": tries}
        return {**asking, "steps": said, "step": "ask_name"}
    return {"base_commit": base, "steps": said, "step": "plan"}
