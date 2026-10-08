from typing import Literal

from app.schemas.common import CamelModel


class HealthRead(CamelModel):
    status: Literal["ok"] = "ok"
