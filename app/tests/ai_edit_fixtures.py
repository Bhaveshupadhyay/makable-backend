"""A small React site in a repo on the fake GitHub, and an "Edit with AI" request on it, shared by the AI edit tests."""

import json
from collections.abc import Callable
from typing import TYPE_CHECKING, Any
from uuid import UUID

from app.api.dependencies import get_github_client
from app.constants.auth import Role
from app.schemas.ai_edit import AiEditRequest
from app.schemas.user import UserRead
from app.services.ai_edit_service import AiEditService
from app.tests.fakes import FakeGithubClient
from app.utils.site_scaffold import manifest_file

PROJECT_ID = UUID("6f1d3c2a-5b4e-4f7a-9c8d-1e2f3a4b5c6d")
USER = UserRead(
    id=UUID("0b8c7d6e-5f4a-4b3c-8d2e-1f0a9b8c7d6e"),
    login="octocat",
    name="The Octocat",
    avatar_url="https://github.com/octocat.png",
    role=Role.USER,
)
REPO_NAME = "my-site"
REPO = f"octocat/{REPO_NAME}"

HERO_PATH = "src/components/Hero.tsx"
HERO = (
    "export function Hero() {\n"
    "  return (\n"
    '    <section id="hero">\n'
    "      <h1>Ada</h1>\n"
    '      <a className="bg-blue-600">Contact</a>\n'
    "    </section>\n"
    "  )\n"
    "}\n"
)
APP = (
    "import { Hero } from './components/Hero'\n"
    "\n"
    "export function App() {\n"
    "  return (\n"
    "    <main>\n"
    "      <Hero />\n"
    "    </main>\n"
    "  )\n"
    "}\n"
)
SITE_FILES: dict[str, str] = {
    **manifest_file(PROJECT_ID),
    "package.json": '{"name": "my-site", "dependencies": {"react": "^19.2.8"}}\n',
    "src/main.tsx": "import { App } from './App'\nimport './index.css'\n",
    "src/App.tsx": APP,
    HERO_PATH: HERO,
    "src/index.css": '@import "tailwindcss";\n\n:root { --bg: #fff; }\n',
}

GOOD_EDITS = [{"path": HERO_PATH, "search": "bg-blue-600", "replace": "bg-emerald-600"}]
GOOD_REPLY = json.dumps({"summary": "Made it green.", "edits": GOOD_EDITS})
GREEN_HERO = HERO.replace("bg-blue-600", "bg-emerald-600")


def add_site_repo(github: FakeGithubClient, files: dict[str, str] | None = None) -> str:
    """Puts the site's repo on the fake GitHub and returns the commit it's at."""
    github.add_repo(REPO_NAME, private=True, files=files if files is not None else SITE_FILES)
    return github.heads[REPO].commit_sha


def _initial_commit() -> str:
    return add_site_repo(FakeGithubClient())


# The fake GitHub's commits are deterministic: this is the site's first commit in every test.
BASE_COMMIT = _initial_commit()


def request_body(**overrides: Any) -> dict[str, Any]:
    """The JSON the SPA sends, camelCase."""
    body: dict[str, Any] = {
        "projectId": str(PROJECT_ID),
        "baseCommit": BASE_COMMIT,
        "instruction": "Make the contact button green",
        "target": {
            "tag": "a",
            "text": "Contact",
            "source": {"path": HERO_PATH, "line": 5, "column": 6},
            "section": {"tag": "section", "id": "hero", "heading": "Ada"},
            "selector": "main > section#hero > a",
            "html": '<a class="bg-blue-600">Contact</a>',
        },
    }
    return {**body, **overrides}


def make_request(**overrides: Any) -> AiEditRequest:
    return AiEditRequest.model_validate(request_body(**overrides))


if TYPE_CHECKING:
    from fastapi.testclient import TestClient

# The `ai_edit_service` fixture: builds the service for a model source (and `debug`).
type MakeAiEditService = Callable[..., AiEditService]


def create_site(client: TestClient) -> str:
    """Creates `PROJECT_ID` through the API for the signed-in user, over its repo on the fake GitHub (taken over: it
    has the site's manifest). Returns the commit the site is at."""
    github = client.app.dependency_overrides[get_github_client]()  # type: ignore[attr-defined]
    add_site_repo(github)
    res = client.post(f"/api/v1/sites/{PROJECT_ID}", json={"name": "My site"})
    assert res.status_code == 201, res.json()
    commit: str = res.json()["data"]["commitSha"]
    return commit


async def confirmed(service: AiEditService, request: AiEditRequest) -> Any:
    """Sends the edit, and reports that the preview works when it asks (what the SPA does when nothing breaks)."""
    return await confirm(service, await service.edit(USER, request))


async def confirm(service: AiEditService, result: Any, project_id: UUID = PROJECT_ID) -> Any:
    """Reports that the preview works, if the run is waiting for it."""
    from app.schemas.ai_edit import AiEditAwaitingResult, AiEditPreviewReport

    if isinstance(result, AiEditAwaitingResult):
        result = await service.report(USER, project_id, AiEditPreviewReport(run_id=result.run_id, ok=True))
    return result
