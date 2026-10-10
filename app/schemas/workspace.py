from typing import Annotated, Any

from pydantic import Field, field_serializer

from app.schemas.common import CamelModel
from app.schemas.session import SessionSnapshot

Sha = Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]


class WorkspaceSession(CamelModel):
    """A session saved in the workspace repo, with the SHA of the file it came from."""

    snapshot: SessionSnapshot
    sha: Sha

    @field_serializer("snapshot")
    def _as_saved(self, snapshot: SessionSnapshot) -> dict[str, Any]:
        # Exactly the fields the file has: the SPA's schema has optional fields that mustn't come back as null.
        return snapshot.model_dump(mode="json", by_alias=True, exclude_unset=True)


class WorkspaceSessionWrite(CamelModel):
    snapshot: SessionSnapshot
    # The SHA of the saved file this one replaces, or None for the first save. A different SHA on GitHub
    # means the session was changed elsewhere, and nothing is overwritten.
    base_sha: Sha | None = None


class WorkspaceSaved(CamelModel):
    sha: Sha
    repo_url: str
