import logging

from app.core.exceptions import ServiceUnavailableError
from app.repositories.health import HealthRepository
from app.schemas.health import HealthRead

logger = logging.getLogger(__name__)


class HealthService:
    def __init__(self, health: HealthRepository) -> None:
        self._health = health

    async def readiness(self) -> HealthRead:
        """Checks the app's dependencies.

        Raises:
            ServiceUnavailableError: The database is unreachable.
        """
        try:
            await self._health.ping()
        except Exception as err:
            logger.warning("readiness check failed", extra={"error": str(err)})
            raise ServiceUnavailableError("Database unavailable") from err
        return HealthRead()
