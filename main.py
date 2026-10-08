"""Entry point: `uv run uvicorn main:create_app --factory --port 8787`."""

from fastapi import FastAPI

from app.api.v1.router import api_router
from app.core.config import Settings, get_settings
from app.core.exceptions import register_exception_handlers
from app.core.lifespan import build_lifespan
from app.core.logging import configure_logging
from app.core.middleware import register_middleware


def create_app(settings: Settings | None = None) -> FastAPI:
    """Builds the app. `settings` configures startup (logging, middleware, connections); requests read
    them through `get_settings`. Tests pass both, see `app/tests/conftest.py`."""
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    app = FastAPI(
        title="makable API",
        version="0.1.0",
        lifespan=build_lifespan(settings),
        # No public API docs in production.
        docs_url=None if settings.environment == "production" else "/api/docs",
        redoc_url=None,
        openapi_url=None if settings.environment == "production" else "/api/openapi.json",
    )
    register_middleware(app, settings)
    register_exception_handlers(app)
    app.include_router(api_router)
    return app
