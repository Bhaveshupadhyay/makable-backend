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

### Google Analytics (optional)

Users connect their own Google Analytics through makable's Google OAuth client. Without one, analytics is off.

1. Google Cloud console → a project → APIs & Services → **Library**: enable the **Google Analytics Admin API** and the
   **Google Analytics Data API**.
2. **Google Auth Platform** (OAuth consent screen): External; app name, support email, home page, privacy policy and
   authorized domain. Data access → add the scope `https://www.googleapis.com/auth/analytics.readonly` (plus `openid`
   and `email`). While the app is in Testing, add yourself under Audience → Test users.
3. Clients → **Create client** → Web application. Authorized redirect URIs:
   `http://localhost:5173/api/v1/analytics/google/callback` (through the Vite proxy) and the production equivalent
   (`APP_URL` + `/api/v1/analytics/google/callback`).
4. Put `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` in `.env`.

`analytics.readonly` is a sensitive scope: before other people can connect, Google has to verify the app (privacy
policy, verified domain, a demo video). Until then the consent screen warns that the app is unverified, at most 100
users can connect, and in Testing mode refresh tokens expire after 7 days (users are asked to connect again).

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

## Analytics

A user connects Google once (`POST /api/v1/analytics/google/connect` returns Google's consent page, read-only
Analytics; the callback keeps the refresh token, encrypted) and picks the GA4 property and web stream each site reports
to (`PUT /api/v1/sites/{projectId}/analytics {propertyId, streamId}`, checked with Google). The site's next publish adds
the stream's tag (gtag.js) to every built page, in the deploy workflow, so the builder's preview never sends visits.
`GET /api/v1/sites/{projectId}/analytics/report?range=7d|28d|90d` reads the stream's numbers from Google's Data API
(visitors, sessions, page views and the days before to compare, day by day, top pages, sources, countries, devices,
people on the site now), kept 10 minutes per worker.

| Endpoint | |
|---|---|
| `GET /api/v1/analytics/google` | `{configured, connected, email}` |
| `POST /api/v1/analytics/google/connect` | `{returnTo}` → `{authorizeUrl}`, sets the state cookie |
| `GET /api/v1/analytics/google/callback` | Google's redirect: back to the SPA with `?analytics=connected` or `?analyticsError=<code>` |
| `DELETE /api/v1/analytics/google` | revokes makable's access and forgets the token (sites keep their streams) |
| `GET /api/v1/analytics/google/properties` | the user's accounts and GA4 properties |
| `GET /api/v1/analytics/google/properties/{id}/streams` | a property's web streams |
| `GET\|PUT\|DELETE /api/v1/sites/{projectId}/analytics` | the site's stream (`link.tagPublished`: its deploy has the tag) |
| `GET /api/v1/sites/{projectId}/analytics/report` | the site's traffic |

Errors: 409 `google_not_connected` / `google_reconnect` (Google refused the stored token, which is then forgotten),
403 `analytics_access_denied`, 404 `analytics_not_linked`, 429 `google_rate_limited`, 503 `analytics_not_configured`.

## Request limits

Bodies are read up to 64 KB (2 MB for AI edits and workspace saves) and refused with 413 past that, before
anything is parsed. Each worker allows 50 AI edits and 100 workspace requests at once (more get 503 with
`Retry-After`), one save at a time per user (bursts of 20, then one per 15 s; more get 429), and 200 outgoing
connections with a 5 s wait for a free one.

## Deploying (Docker + Reverse Proxy on Hostinger VPS)

`compose.yaml` runs the API in a container published on `127.0.0.1:8787` with 16–24 GB RAM allocated, behind CloudPanel's
Nginx reverse proxy with Let's Encrypt SSL. The API is only reachable locally on the server (never exposed directly on
host ports), so CloudPanel keeps full control over ports 80/443 and other sites on the VPS.

`.github/workflows/ci-cd.yml` checks every pull request and push to `main` (ruff, mypy, pytest, `alembic check`, and
an image build). Merging to `main` doesn't deploy. To deploy `main`, either push a commit whose message has `[deploy]`:

```sh
git switch main && git pull
git commit --allow-empty -m "Deploy [deploy]" && git push
```

or run the workflow by hand (Actions → CI/CD → Run workflow on `main`, or `gh workflow run ci-cd.yml --ref main`).
A deploy runs the checks, pushes the image to `ghcr.io/bhaveshupadhyay/makable-backend` (tagged with the commit and
`latest`), then over SSH copies `compose.yaml` to the server, pulls the image, runs `alembic upgrade head` against
the production database and restarts, waiting until the API reaches the database and passes its health check. A run
for an older commit than the one live (`deployed-sha` on the server) skips the rollout instead of rolling back.

### Once, on the server

1. Point your API domain's DNS `A` record (e.g. `api.example.com`) to the VPS IP, and wait until it resolves:
   Let's Encrypt checks the domain from outside, so the certificate fails before then.
2. In CloudPanel (or Nginx), create a **Reverse Proxy site** for that domain forwarding to `http://127.0.0.1:8787`,
   then enable Let's Encrypt SSL under the site's SSL/TLS tab.
3. In the site's Vhost, raise the proxy timeouts inside `location /`. Nginx's default of 60 s cuts off AI edits, which
   can make two model calls of up to `AI_TIMEOUT_SECONDS` (90 s) each:

   ```nginx
   proxy_read_timeout 300s;
   proxy_send_timeout 300s;
   ```

   The API trusts `X-Forwarded-For` only from its Docker network's gateway (`FORWARDED_ALLOW_IPS` in `compose.yaml`).
   If the logs show `172.31.87.1` instead of visitors' IPs, check that the Vhost sends that header.
4. Install Docker (`curl -fsSL https://get.docker.com | sh`) and add a deploy user that may use it (the `docker`
   group is as powerful as root, so use this user for nothing else):

   ```sh
   adduser --disabled-password --gecos "" deploy
   usermod -aG docker deploy
   install -d -o deploy -g deploy -m 750 /opt/makable-backend
   ```

5. Write `/opt/makable-backend/.env` (owner `deploy`, `chmod 600`) with the production values:
   `APP_URL=https://<the SPA's domain>` and the same `TOKEN_ENCRYPTION_KEY` as before (stored tokens can't be read
   with another). `ENVIRONMENT=production` is set by compose. Write a literal `$` as `$$`.
6. Make an SSH key for GitHub (`ssh-keygen -t ed25519 -f makable-deploy -N ""` on your machine) and append
   `makable-deploy.pub` to `/home/deploy/.ssh/authorized_keys`.
7. Supabase → Redirect URLs: add `https://<the SPA's domain>/api/v1/auth/github/callback`.

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

Add the **variable** `API_URL` (e.g. `https://api.example.com`) in the same environment: after the rollout the
deploy checks `/api/v1/health/ready` through it, so a broken DNS, Nginx or certificate fails the run. Without it the
check is skipped with a warning.

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
