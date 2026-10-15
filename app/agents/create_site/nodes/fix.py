from langgraph.runtime import Runtime

from app.agents.create_site.nodes.progress import progress
from app.agents.create_site.prompts.build import fix_prompt
from app.agents.create_site.prompts.system import CREATE_SYSTEM
from app.agents.create_site.state import CreateRun, CreateState
from app.agents.create_site.tools.code import extract_code
from app.schemas.chat import ChatMessage


async def fix(state: CreateState, runtime: Runtime[CreateRun]) -> CreateState:
    """Asks the model to rewrite each file the checks found problems in, with the problems. A reply without a file
    leaves the file as it was (the next check reports it again)."""
    run = runtime.context
    model = await run.model_client()
    files = await run.site_files(state["base_commit"])
    attempt = state["attempt"]
    for path, problems in state["problems"].items():
        progress("fix", f"Fixing {path}", path=path)
        text = f"{CREATE_SYSTEM}\n\n{fix_prompt(path, files.get(path, ''), problems)}"
        run.model_input = text
        reply = await model.complete([ChatMessage(role="user", content=text)])
        attempt += 1
        try:
            run.files[path] = extract_code(reply)
        except ValueError:
            continue
    written = [*state["written"], *(p for p in state["problems"] if p not in state["written"])]
    return {"fix_rounds": state["fix_rounds"] + 1, "attempt": attempt, "written": written, "step": "verify"}
