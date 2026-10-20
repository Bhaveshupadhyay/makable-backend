from fastapi import APIRouter

from app.api.v1.endpoints import ai_create, ai_edit, ai_keys, analytics, auth, health, publish, sites
from app.constants.api import API_V1_PREFIX

api_router = APIRouter(prefix=API_V1_PREFIX)
api_router.include_router(ai_create.router)
api_router.include_router(ai_edit.router)
api_router.include_router(ai_keys.router)
api_router.include_router(analytics.router)
api_router.include_router(analytics.callback_router)
api_router.include_router(auth.router)
api_router.include_router(health.router)
api_router.include_router(publish.router)
api_router.include_router(sites.router)
