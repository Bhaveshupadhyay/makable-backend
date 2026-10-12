from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, status

from app.api.dependencies import AiKeyServiceDep, CurrentUserDep, ai_key_check_limit
from app.schemas.ai_key import AiKeyChecked, AiKeyCreate, AiKeyRead, AiKeyUpdate, AiModelOption, AiProviderRead
from app.schemas.common import ApiResponse, ErrorResponse

router = APIRouter(prefix="/ai", tags=["ai"])

ERRORS: dict[int | str, dict[str, Any]] = {
    status.HTTP_401_UNAUTHORIZED: {"model": ErrorResponse},
    status.HTTP_404_NOT_FOUND: {"model": ErrorResponse},
}
CHECK_ERRORS: dict[int | str, dict[str, Any]] = {
    **ERRORS,
    status.HTTP_422_UNPROCESSABLE_CONTENT: {"model": ErrorResponse},
    status.HTTP_429_TOO_MANY_REQUESTS: {"model": ErrorResponse},
    status.HTTP_502_BAD_GATEWAY: {"model": ErrorResponse},
}


@router.get("/providers", summary="AI providers a key can be added for")
async def list_providers(_: CurrentUserDep, keys: AiKeyServiceDep) -> ApiResponse[list[AiProviderRead]]:
    return ApiResponse(data=keys.providers())


@router.get("/keys", summary="The user's AI provider keys", responses=ERRORS)
async def list_keys(user: CurrentUserDep, keys: AiKeyServiceDep) -> ApiResponse[list[AiKeyRead]]:
    """Only each key's last characters are sent back, never the key."""
    return ApiResponse(data=await keys.list_keys(user.id))


@router.post(
    "/keys",
    summary="Add or replace a key",
    status_code=status.HTTP_201_CREATED,
    responses=CHECK_ERRORS,
    dependencies=[Depends(ai_key_check_limit)],
)
async def add_key(body: AiKeyCreate, user: CurrentUserDep, keys: AiKeyServiceDep) -> ApiResponse[AiKeyChecked]:
    """Checks the key with the provider (422 `ai_key_invalid` if it's refused) and keeps it encrypted, replacing
    the user's key for that provider. Returns every chat model the key can use, to pick from."""
    return ApiResponse(data=await keys.add_key(user.id, body))


@router.patch("/keys/{key_id}", summary="Pick the models a key shows in the chat", responses=ERRORS)
async def update_key(
    key_id: UUID, body: AiKeyUpdate, user: CurrentUserDep, keys: AiKeyServiceDep
) -> ApiResponse[AiKeyRead]:
    return ApiResponse(data=await keys.update_key(user.id, key_id, body))


@router.post(
    "/keys/{key_id}/check",
    summary="Check a key again",
    responses=CHECK_ERRORS,
    dependencies=[Depends(ai_key_check_limit)],
)
async def check_key(key_id: UUID, user: CurrentUserDep, keys: AiKeyServiceDep) -> ApiResponse[AiKeyChecked]:
    """Asks the provider whether the key still works. A refused key comes back with `status: "invalid"`."""
    return ApiResponse(data=await keys.check_key(user.id, key_id))


@router.delete("/keys/{key_id}", summary="Remove a key", status_code=status.HTTP_204_NO_CONTENT, responses=ERRORS)
async def delete_key(key_id: UUID, user: CurrentUserDep, keys: AiKeyServiceDep) -> None:
    await keys.delete_key(user.id, key_id)


@router.get("/models", summary="The chat's model menu", responses=ERRORS)
async def list_models(user: CurrentUserDep, keys: AiKeyServiceDep) -> ApiResponse[list[AiModelOption]]:
    """makable's own model, then the models the user turned on for each working key."""
    return ApiResponse(data=await keys.model_options(user.id))
