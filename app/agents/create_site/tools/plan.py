"""The model's plan for a new site: a one-line summary and the files it will write, each with what it's for."""

from typing import Annotated

from pydantic import AfterValidator, Field, StringConstraints, TypeAdapter

from app.agents.ai_edit.tools.answer import extract_json
from app.constants.ai_create import CREATE_PATH, MAX_CREATE_FILES
from app.schemas.common import CamelModel


def _create_path(value: str) -> str:
    if not CREATE_PATH.fullmatch(value):
        raise ValueError("must be a .tsx, .ts or .css file under src/ (not src/main.tsx)")
    return value


class PlannedFileModel(CamelModel):
    path: Annotated[str, AfterValidator(_create_path)]
    purpose: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)]


def _app_included(files: list[PlannedFileModel]) -> list[PlannedFileModel]:
    paths = [f.path for f in files]
    if "src/App.tsx" not in paths:
        raise ValueError("must include src/App.tsx (the page main.tsx renders)")
    if len(set(paths)) != len(paths):
        raise ValueError("lists a file more than once")
    return files


class SitePlan(CamelModel):
    summary: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
    files: Annotated[
        list[PlannedFileModel], Field(min_length=1, max_length=MAX_CREATE_FILES), AfterValidator(_app_included)
    ]


_PLAN: TypeAdapter[SitePlan] = TypeAdapter(SitePlan)


def parse_plan(reply: str) -> SitePlan:
    """The plan in the model's reply.

    Raises:
        ValueError: No JSON, or not a valid plan (`ValidationError` is a `ValueError`).
    """
    return _PLAN.validate_python(extract_json(reply))
