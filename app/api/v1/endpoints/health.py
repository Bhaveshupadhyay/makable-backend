from fastapi import APIRouter, status

from app.api.dependencies import HealthServiceDep
from app.schemas.common import ApiResponse, ErrorResponse
from app.schemas.health import HealthRead

router = APIRouter(prefix="/health", tags=["health"])


@router.get("/live", summary="Liveness probe")
async def live() -> ApiResponse[HealthRead]:
    """The process is up. Checks nothing else, so a slow database never gets the app restarted."""
    return ApiResponse(data=HealthRead())


@router.get(
    "/ready",
    summary="Readiness probe",
    responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ErrorResponse}},
)
async def ready(health: HealthServiceDep) -> ApiResponse[HealthRead]:
    """The app can serve traffic: the database is reachable."""
    return ApiResponse(data=await health.readiness())
