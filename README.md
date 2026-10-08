# makable-backend

The API for [makable](../makable): FastAPI, async SQLAlchemy, Alembic, managed with [uv](https://docs.astral.sh/uv/).

## Setup

```sh
uv sync
cp .env.example .env          # then fill in the secrets
uv run alembic upgrade head
uv run uvicorn main:create_app --factory --reload --port 8787
```

The SPA's Vite dev server proxies `/api` to `localhost:8787`. API docs: http://localhost:8787/api/docs.

### Database

Postgres through asyncpg, configured with `DB_*` in `.env` (or a full `DATABASE_URL`, e.g. SQLite for offline dev).
`app/core/client.py` is the Postgres client: one engine (connection pool) and session factory per process, created
lazily. The lifespan calls `open_connection`/`close_connection`; requests get sessions via `get_db_session`, Alembic
via `get_postgres_engine`.

- Pool: `DB_POOL_SIZE` kept open + `DB_MAX_OVERFLOW` extra under load, `pool_pre_ping` (a connection the pooler
  dropped is replaced, not handed to a request), recycled every `DB_POOL_RECYCLE` seconds.
- Total connections = uvicorn workers x (pool size + overflow). Keep it under your Supabase plan's pooler limit.
- Supabase session mode (port 5432) works as is and is the recommended setup. For transaction mode (port 6543) set `DB_TRANSACTION_POOLER=true`,
  which turns off asyncpg's prepared statement cache.

### Sign-in (Supabase Auth + GitHub)

Supabase runs the GitHub OAuth flow. The GitHub side is an **OAuth App** (not a GitHub App), because we ask for the
`repo` scope to create repositories and push commits for the user.

1. GitHub → Settings → Developer settings → **OAuth Apps** → New. Authorization callback URL:
   `https://<project-ref>.supabase.co/auth/v1/callback`.
2. Supabase → Authentication → Sign In / Providers → **GitHub**: enable it with that OAuth App's client ID and secret.
3. Supabase → Authentication → URL Configuration → **Redirect URLs**: add
   `http://localhost:5173/api/v1/auth/github/callback` (and the production equivalent).
4. Supabase → Project Settings → **JWT Keys**: the project must use asymmetric signing keys (the default for new
   projects). The API verifies access tokens against the published keys and rejects legacy HS256 tokens.
5. Put `SUPABASE_URL` and `SUPABASE_PUBLISHABLE_KEY` in `.env`.

## Architecture

Router → Service → Repository → Database.

```
main.py              create_app() factory
app/
  api/
    dependencies.py  DI wiring: the only place that picks implementations
    cookies.py       auth cookie flags
    v1/endpoints/    routers: HTTP in/out only, no business logic
  core/              config, client (Postgres), database (Base), security, logging, exceptions, middleware, lifespan
  models/            SQLAlchemy models (UUID primary keys)
  schemas/           Pydantic models: Create/Update/Read, envelopes
  repositories/      CRUD only. A Protocol + a SQLAlchemy implementation each. Never commit
  services/          business logic. Own the transaction via UnitOfWork
  clients/           external APIs (Supabase Auth, GitHub)
  utils/ constants/
  tests/             api/, services/, repositories/, utils/
alembic/             migrations
```

- Services depend on repository/client **Protocols**; `api/dependencies.py` injects the concrete ones. Tests swap them with `app.dependency_overrides`.
- Responses are wrapped: `{"success": true, "data": ...}` or `{"success": false, "error": {"code", "message", "details"}, "requestId"}`.
- Domain errors subclass `AppError` (`core/exceptions.py`) and are mapped to status codes by global handlers. Unhandled errors return a generic 500; the stack trace only goes to the logs.
- Logs are JSON on stdout, each tagged with the request id (`X-Request-ID`, echoed in responses).

## Auth

GitHub sign-in through Supabase Auth (authorization code + PKCE, run by the backend). Supabase issues the session,
and the backend keeps it in cookies.

| Endpoint | |
|---|---|
| `GET /api/v1/auth/github/login?returnTo=/path` | redirects to Supabase, which sends the user on to GitHub. The PKCE verifier and `returnTo` go in a signed 10-minute cookie |
| `GET /api/v1/auth/github/callback` | redeems the code with Supabase, upserts the user, stores their GitHub token (Fernet-encrypted), sets the session cookies, redirects to `returnTo`. Failures redirect with `?authError=<code>` |
| `GET /api/v1/auth/session` | `{data: {user: {id, login, name, avatarUrl, role}}}` or 401 |
| `POST /api/v1/auth/refresh` | refreshes the Supabase session (the refresh token rotates). 204 or 401 |
| `POST /api/v1/auth/logout` | signs the session out in Supabase, clears cookies. 204 |

Tokens never reach JavaScript: both live in HttpOnly, SameSite=Lax cookies.

- **Access token:** Supabase's JWT (the project's JWT expiry, 1 hour by default), path `/api/v1`. Verified locally
  against Supabase's cached signing keys, so a request costs one database query and no call to Supabase.
- **Refresh token:** Supabase's, path `/api/v1/auth`. Single-use, rotated on every refresh, revoked on logout.
- **GitHub token:** Supabase hands it over only once, at sign-in, and doesn't keep it. We store it encrypted in
  `github_credentials`. OAuth App tokens don't expire. If the user revokes the app, they have to sign in again.

Users are matched by `users.auth_user_id` (Supabase's `auth.users.id`, the JWT `sub`).

Roles: `require_roles(Role.ADMIN)` in `api/dependencies.py` guards a route.

## Checks

```sh
uv run pytest
uv run ruff check . && uv run ruff format --check .
uv run mypy .
uv run alembic check          # models and migrations agree
uv run pre-commit install     # run all of the above on commit
```

New migration after changing a model: `uv run alembic revision --autogenerate -m "..."`.
