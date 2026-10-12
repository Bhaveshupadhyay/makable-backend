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

## AI edits

`POST /api/v1/ai/edit` (signed in) takes the SPA's `AiEditRequest`: the instruction, the element the user selected in
the preview, the template, and the files most likely to change. It answers `{tier: 1, summary, edits}` (search/replace
edits the SPA applies) or `{tier: 2, reason}` (the change needs a deeper edit, which isn't built yet).

- Requests that are clearly site-wide ("add a page", "install") go to Tier 2 without a model call.
- Otherwise the prompt (server-only) goes to an OpenAI-compatible model (`AI_*` in `.env`; a local OmniRoute by
  default) as one user turn.
- The answer's edits are dry-run before they're sent: only files that were sent, never `package.json`, lockfiles or
  `.github/`; each search must match exactly once; the content file must still be a valid portfolio; scripts must
  parse (tree-sitter); CSS braces must balance. A rejected answer is retried once with the error, then it's a 502
  `ai_edit_rejected`. An unreachable model is a 502 `ai_unavailable`.
- `history`: up to 10 earlier requests (instruction, selection, what happened) that the browser keeps and sends back, so
  follow-ups like "make it bigger" work. The server stores no conversation.
- If the browser disconnects (the user pressed Stop), the model call is cancelled.
- `AI_DEBUG=true` adds `debug: {modelInput, attempts, model}` to the response.

## Workspace sync

Signed-in users' builder sessions are saved to a private `makable-workspace` repo on their GitHub account, with their
own token. Each site is a folder, `projects/<projectId>/`, split into files so a save only writes what changed:
`state.json`, `messages/NNNN.json` (chat chunks), `ai-history/<template>.json` and `files/<template>/<path>`. Each save
is one commit. `latest.json` at the root names the site saved last.

| Endpoint | |
|---|---|
| `PUT /api/v1/workspace/sessions/{projectId}` | `{baseSha, state, parts, deletes}`: writes the changed files and the state as one commit, creating the repo on first use. 409 `workspace_conflict` when the state changed since `baseSha` |
| `GET /api/v1/workspace/sessions/latest` | the newest saved state (`{state, sha}`), or 404 `workspace_session_not_found` |
| `GET /api/v1/workspace/sessions/{projectId}` | one site's saved state |
| `GET /api/v1/workspace/sessions/{projectId}/parts/{path}` | one file of a saved session |

makable never writes to a `makable-workspace` repo it didn't create (no marker file) or one that's public.

## Request limits

Bodies are read up to 64 KB (2 MB for AI edits and workspace saves) and refused with 413 past that, before
anything is parsed. Each worker allows 50 AI edits and 100 workspace requests at once (more get 503 with
`Retry-After`), one save at a time per user (bursts of 20, then one per 15 s; more get 429), and 200 outgoing
connections with a 5 s wait for a free one.

## Deploying (Docker + Cloudflare Tunnel)

`compose.yaml` runs the API in a container and connects it to Cloudflare with a tunnel, so it can share a VPS with
other sites (e.g. CloudPanel's Nginx keeps ports 80/443). The API isn't published on any host port.

`.github/workflows/ci-cd.yml` checks every push and pull request (ruff, mypy, pytest, `alembic check`, and an image
build). Merging to `main` doesn't deploy. To deploy `main`, either push a commit whose message has `[deploy]`:

```sh
git switch main && git pull
git commit --allow-empty -m "Deploy [deploy]" && git push
```

or run the workflow by hand (Actions → CI/CD → Run workflow on `main`, or `gh workflow run ci-cd.yml --ref main`).
A deploy runs the checks, pushes the image to `ghcr.io/bhaveshupadhyay/makable-backend` (tagged with the commit and
`latest`), then over SSH copies `compose.yaml` to the server, pulls the image, runs `alembic upgrade head` against
the production database and restarts, waiting for the health check.

### Once, on the server

1. Cloudflare → Zero Trust → Networks → **Tunnels** → create a `cloudflared` tunnel and copy its token. Add a public
   hostname, e.g. `api.example.com`, with service `http://api:8787`.
2. Install Docker (`curl -fsSL https://get.docker.com | sh`) and add a deploy user that may use it (the `docker`
   group is as powerful as root, so use this user for nothing else):

   ```sh
   adduser --disabled-password --gecos "" deploy
   usermod -aG docker deploy
   install -d -o deploy -g deploy -m 750 /opt/makable-backend
   ```

3. Write `/opt/makable-backend/.env` (owner `deploy`, `chmod 600`) with the production values:
   `APP_URL=https://<the SPA's domain>`, the same `TOKEN_ENCRYPTION_KEY` as before (stored tokens can't be read
   with another), and `TUNNEL_TOKEN`. `ENVIRONMENT=production` is set by compose. Write a literal `$` as `$$`.
4. Make an SSH key for GitHub (`ssh-keygen -t ed25519 -f makable-deploy -N ""` on your machine) and append
   `makable-deploy.pub` to `/home/deploy/.ssh/authorized_keys`.
5. Supabase → Redirect URLs: add `https://<the SPA's domain>/api/v1/auth/github/callback`.

### Once, on GitHub

Repo → Settings → Environments → new environment **`production`** (add required reviewers to approve each deploy),
with these secrets:

| Secret | Value |
| --- | --- |
| `VPS_HOST` | the server's IP or hostname |
| `VPS_PORT` | SSH port, if not 22 |
| `VPS_USER` | `deploy` |
| `VPS_SSH_KEY` | the private key `makable-deploy` |
| `VPS_KNOWN_HOSTS` | output of `ssh-keyscan -p <port> <host>` (check it against the server's real host key) |

The server pulls with the job's own token and logs out at the end, so it keeps no registry password.

### By hand

```sh
cd /opt/makable-backend
docker compose logs -f api
API_IMAGE=ghcr.io/bhaveshupadhyay/makable-backend:<commit sha> docker compose up -d   # roll back
```

Images of the last two weeks stay on the server for rollbacks. A rollback doesn't undo migrations. The container runs
one uvicorn worker, because the request limits are per process.

The SPA must reach the API on its own origin (the auth cookies are SameSite=Lax with path `/api/v1`), so its host
forwards `/api/*` to `https://api.example.com`, as the Vite proxy does in dev.

## Checks

```sh
uv run pytest
uv run ruff check . && uv run ruff format --check .
uv run mypy .
uv run alembic check          # models and migrations agree
uv run pre-commit install     # run all of the above on commit
```

New migration after changing a model: `uv run alembic revision --autogenerate -m "..."`.
