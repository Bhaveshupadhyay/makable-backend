import pytest

from app.agents.create_site.prompts import build
from app.agents.create_site.prompts.build import write_prompt
from app.agents.create_site.state import PlannedFile

PLAN = [
    PlannedFile(path="src/data.ts", purpose="the classes"),
    PlannedFile(path="src/components/Schedule.tsx", purpose="the timetable"),
    PlannedFile(path="src/App.tsx", purpose="the page"),
]
# Longer than the 5,000 characters past which a file used to be shown as only the names it exports.
DATA = "export const classes = [\n" + "  { title: 'WOD', coach: 'Ada' },\n" * 200 + "]\n"
SCHEDULE = (
    "interface ScheduleProps { onClose: () => void }\n"
    "export function Schedule({ onClose }: ScheduleProps) {\n"
    "  return <section />\n"
    "}\n"
)
WRITTEN = {"src/data.ts": DATA, "src/components/Schedule.tsx": SCHEDULE}


def _prompt(written: dict[str, str]) -> str:
    return write_prompt("A gym", "Core", [], PLAN, PLAN[2], written, {})


def test_the_files_written_are_shown_whole_while_they_fit() -> None:
    prompt = _prompt(WRITTEN)

    assert f'<files_written>\n<file path="src/data.ts">\n{DATA}</file>\n\n' in prompt
    assert f'<file path="src/components/Schedule.tsx">\n{SCHEDULE}</file>\n</files_written>' in prompt
    assert 'outline="true"' not in prompt
    assert "give a component the props its signature takes, and read data with the fields and shape it has" in prompt
    assert "<files_written>" not in _prompt({})


def test_a_file_past_what_fits_is_shown_as_its_outline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(build, "WRITTEN_SHOWN_CHARS", len(SCHEDULE))

    prompt = _prompt(WRITTEN)

    # data.ts doesn't fit, so it's outlined; Schedule.tsx, written after it, still fits whole.
    assert (
        '<file path="src/data.ts" outline="true">\n'
        "export const classes = [{ title: 'WOD', coach: 'Ada' }, /* …199 more */]\n"
        "</file>"
    ) in prompt
    assert f'<file path="src/components/Schedule.tsx">\n{SCHEDULE}</file>' in prompt
    assert 'A file with outline="true" is too long to show whole' in prompt
