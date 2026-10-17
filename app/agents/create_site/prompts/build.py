"""The create agent's prompts: the plan, each file, and the fixes the checks ask for."""

from app.agents.create_site.state import PlannedFile
from app.agents.create_site.tools.brief import brief_block
from app.agents.create_site.tools.checks import exported_names
from app.agents.thread import QuestionAnswer
from app.constants.ai_create import MAX_CREATE_FILES, SHOWN_WHOLE_CHARS


def plan_prompt(prompt: str, name: str, brief: list[QuestionAnswer]) -> str:
    return f"""\
<site_name>{name}</site_name>
<description>
{prompt}
</description>
{_answers(brief)}
Plan the site's files. Reply with ONLY one JSON object, no prose:
{{"summary": "<one sentence for the user: the site you'll build>", "files": [{{"path": "src/...", "purpose": \
"<what it holds: its sections, data or styles>"}}]}}

- Include src/App.tsx (the page: it puts the sections together) and src/index.css (the site's colours, fonts and base \
styles). Then one file per section or shared component, under src/components/, and src/data.ts if the content is \
best kept as data (menus, products, services...).
- At most {MAX_CREATE_FILES} files. Paths end in .tsx, .ts or .css. Don't list src/main.tsx.
- List files so that each one only imports files listed before it (App.tsx last).
- The user's answers to makable's questions (<user_answers>, if there are any) say what kind of site it is and \
what it needs: plan that site.
- {_name_rule(name)}"""


def write_prompt(
    prompt: str,
    name: str,
    brief: list[QuestionAnswer],
    plan: list[PlannedFile],
    file: PlannedFile,
    written: dict[str, str],
    base: dict[str, str],
) -> str:
    planned = "\n".join(f"- {f['path']}: {f['purpose']}" for f in plan)
    shown = "\n\n".join(_show(path, code) for path, code in written.items())
    context = f"<files_written>\n{shown}\n</files_written>\n\n" if shown else ""
    css = (
        "" if "src/index.css" in written else f'<file path="src/index.css">\n{base.get("src/index.css", "")}</file>\n\n'
    )
    return f"""\
<site_name>{name}</site_name>
<description>
{prompt}
</description>
{_answers(brief)}
<plan>
{planned}
</plan>

{context}{css}Write {file["path"]}: {file["purpose"]}

{_name_rule(name)} Reply with the complete file in one ``` code block and nothing else. Import only files from the \
plan (with the names they export), react, and lucide-react icons."""


def fix_prompt(path: str, code: str, problems: list[str]) -> str:
    listed = "\n".join(f"- {p}" for p in problems)
    return f"""\
{path} was checked before the site was saved, and it has problems that would break the preview:
{listed}

<file path="{path}">
{code}</file>

Reply with the complete corrected file in one ``` code block and nothing else. Keep everything else as it is."""


def _name_rule(name: str) -> str:
    """The site's name is the user's choice: the code uses it, not a name from the description."""
    return (
        f"The site is called {name} (<site_name>, the name the user chose): wherever the site's name appears (the "
        "brand in the header, the <title> in App, the footer), write it exactly as written there, as text in the "
        "code, even if the description calls the site something else."
    )


def _answers(brief: list[QuestionAnswer]) -> str:
    """The user's answers to the questions about the site, after the description (a blank line when there are none)."""
    block = brief_block(brief)
    return f"\n{block}" if block else ""


def _show(path: str, code: str) -> str:
    """A file written earlier: whole when it's short, else what it exports (what later files can import)."""
    if len(code) <= SHOWN_WHOLE_CHARS:
        return f'<file path="{path}">\n{code}</file>'
    exports = ", ".join(sorted(exported_names(code))) or "nothing"
    return f'<file path="{path}" exports="{exports}" />'
