# syntax=docker/dockerfile:1

# Build: install the locked dependencies into /app/.venv. Every native package (asyncpg, cryptography, PyNaCl,
# tree-sitter) ships a Linux wheel, so no compiler is needed.
FROM python:3.14-slim-trixie AS build
COPY --from=ghcr.io/astral-sh/uv:0.11.21 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never
WORKDIR /app
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-dev --no-install-project

# Run: the same Python, the venv and the app code. No uv, no tests, no secrets (.env comes from compose).
FROM python:3.14-slim-trixie
# Links the GHCR package to the repo; the deploy also prunes old images by it.
LABEL org.opencontainers.image.source=https://github.com/Bhaveshupadhyay/makable-backend
RUN useradd --system --uid 10001 --no-create-home makable
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
COPY alembic.ini main.py ./
COPY alembic ./alembic
COPY app ./app
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
USER makable
EXPOSE 8787

# Healthy means ready: the database answers. Docker doesn't restart an unhealthy container, so a database outage
# only shows here (and fails a deploy's `compose up --wait`); it never restarts the app.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8787/api/v1/health/ready', timeout=4)"]

# One worker: the request limits (core/limits.py) are counted per process.
# Forwarded headers are trusted only from FORWARDED_ALLOW_IPS (uvicorn's default 127.0.0.1 when unset); compose.yaml
# sets it to the reverse proxy's address, so the logs show the visitor's IP and a caller can't fake it.
CMD ["uvicorn", "main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8787", "--workers", "1", \
     "--proxy-headers"]
