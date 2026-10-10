from fastapi import APIRouter

from app.api.v1.endpoints import ai_edit, auth, health, workspace
from app.constants.api import API_V1_PREFIX

api_router = APIRouter(prefix=API_V1_PREFIX)
api_router.include_router(ai_edit.router)
api_router.include_router(auth.router)
api_router.include_router(health.router)
api_router.include_router(workspace.router)
