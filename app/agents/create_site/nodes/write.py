from langgraph.runtime import Runtime

from app.agents.create_site.nodes.progress import progress
from app.agents.create_site.prompts.build import write_prompt
from app.agents.create_site.prompts.system import CREATE_SYSTEM
from app.agents.create_site.state import CreateFailedError, CreateRun, CreateState
from app.agents.create_site.tools.code import extract_code
from app.constants.ai_create import MAX_CREATED_FILE_CHARS, MAX_FILE_ATTEMPTS, REJECTED_LINE_CHARS
from app.schemas.chat import ChatMessage
from app.utils.syntax_check import locate_syntax_error, script_language


async def write(state: CreateState, runtime: Runtime[CreateRun]) -> CreateState:
    """Writes the plan's next file (one per step, so the browser hears about each). The file is kept in the run's
    context, not the state, until the commit. A reply without a usable file is asked for again once.

    Raises:
        CreateFailedError: The model didn't write the file in its tries.
    """
    run = runtime.context
    model = await run.model_client()
    plan, index = state["plan"], state["index"]
    file = plan[index]
    path = file["path"]
    said = progress("write", f"Writing {path}", path=path, done=index, total=len(plan))
    base = await run.base_files(state["base_commit"])
    asked = state["brief"]
    text = f"{CREATE_SYSTEM}\n\n{write_prompt(state['prompt'], state['name'], asked, plan, file, run.files, base)}"
    run.model_input = text
    messages = [ChatMessage(role="user", content=text)]
    attempt = state["attempt"]
    for _ in range(MAX_FILE_ATTEMPTS):
        reply = (await model.complete(messages)).text
        attempt += 1
        try:
            code = _checked(path, extract_code(reply))
        except ValueError as err:
            messages += [
                ChatMessage(role="assistant", content=reply),
                ChatMessage(role="user", content=f"That reply was rejected: {err}. Reply with the complete file."),
            ]
            continue
        run.files[path] = code
        break
    else:
        raise CreateFailedError(f"the model couldn't write {path}")
    nxt = index + 1
    return {
        "index": nxt,
        "written": [*state["written"], path],
        "attempt": attempt,
        "model": model.name,
        "usage": run.usage.total(),
        "steps": said,
        "step": "write" if nxt < len(plan) else "verify",
    }


def _checked(path: str, code: str) -> str:
    if len(code) > MAX_CREATED_FILE_CHARS:
        raise ValueError(
            f"the file is too long ({len(code)} characters): split it, or keep it under {MAX_CREATED_FILE_CHARS}"
        )
    language = script_language(path)
    syntax = locate_syntax_error(code, language) if language is not None else None
    if syntax:
        # The line itself, so the model can see what to change (the error alone, it sends the same file back).
        raise ValueError(f"it doesn't parse: {syntax[0]}, in ⟦{_line_at(code, syntax[1])}⟧")
    return code


def _line_at(code: str, offset: int) -> str:
    """The line at `offset`, cut to `REJECTED_LINE_CHARS` around it. An error at the end of the file (a missing `}`)
    quotes the last line with text."""
    if not code[offset:].strip():
        offset = len(code[:offset].rstrip())
    start = code.rfind("\n", 0, offset) + 1
    end = code.find("\n", offset)
    line = code[start : len(code) if end == -1 else end]
    cut = max(0, min(offset - start - REJECTED_LINE_CHARS // 2, len(line) - REJECTED_LINE_CHARS))
    return line[cut : cut + REJECTED_LINE_CHARS].strip()
