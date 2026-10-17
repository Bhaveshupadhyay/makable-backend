"""The deploy workflow a publish writes to `.github/workflows/makable-deploy.yml`: build the site (React sites with
Vite, static ones as they are) and deploy it to the platform the user picked.

It only runs when started (`workflow_dispatch`): by makable when the user clicks Publish, or by hand from the
Actions tab. A push doesn't deploy, so a change made on GitHub stays unpublished until the user publishes.

Values that come from the user or GitHub (project name, branch, base path) only go into `env:` as JSON strings,
which YAML reads as quoted scalars, and scripts read them as quoted shell variables: they can't change the
workflow or a command. A platform's project is found or created by name in the workflow too, so a token the
user added on GitHub themselves works the same as one they pasted in the chat.
"""

import json

from app.constants.publish import TARGET_NAMES, TARGET_SECRETS, PublishTarget, SiteKind

# Pinned by major version; bump them here.
CHECKOUT = "actions/checkout@v4"
SETUP_NODE = "actions/setup-node@v4"
NODE_VERSION = "22"
CONFIGURE_PAGES = "actions/configure-pages@v5"
UPLOAD_PAGES_ARTIFACT = "actions/upload-pages-artifact@v3"
DEPLOY_PAGES = "actions/deploy-pages@v4"
WRANGLER_ACTION = "cloudflare/wrangler-action@v3"
VERCEL_CLI = "vercel@latest"
NETLIFY_CLI = "netlify-cli@latest"

# Every build puts the site in this folder.
OUT_DIR = "dist"


def render_workflow(
    target: PublishTarget, kind: SiteKind, *, branch: str, base_path: str, project_name: str | None
) -> str:
    """The workflow file for a site. `base_path` is where the site is served (`/`, or `/<repo>/` for a GitHub
    Pages project site); `project_name` is the platform project (None for GitHub Pages)."""
    env = {"BRANCH": branch, "BASE_PATH": base_path}
    if project_name is not None:
        env["PROJECT_NAME"] = project_name
    pages = target == PublishTarget.GITHUB_PAGES
    permissions = ["contents: read", *(["pages: write", "id-token: write"] if pages else [])]
    header = f"""\
# Deploys this site to {TARGET_NAMES[target]} when makable publishes it (or when you run it from the Actions tab).
# Pushing doesn't deploy: changes made on GitHub go live with the next publish. makable writes this file on every
# publish, so changes made here are replaced by the next publish.
name: Deploy to {TARGET_NAMES[target]}

on:
  workflow_dispatch:

concurrency:
  group: makable-deploy
  cancel-in-progress: true

permissions:
{_lines(permissions, 2)}

env:
{_lines([f"{name}: {json.dumps(value)}" for name, value in env.items()], 2)}

jobs:
"""
    build = f"      - uses: {CHECKOUT}\n" + (_REACT_BUILD if kind == "react" else _STATIC_BUILD)
    if pages:
        return header + _PAGES_JOB.format(build=build)
    return header + _check_job(TARGET_SECRETS[target]) + _DEPLOY_JOB.format(build=build) + _DEPLOY_STEPS[target]


def _lines(items: list[str], indent: int) -> str:
    return "\n".join(" " * indent + item for item in items)


def _check_job(secrets: tuple[str, ...]) -> str:
    """A job that says which secrets are missing instead of failing: the first push comes before the user had a
    repo to add them to. The deploy job is skipped until they're there."""
    env = _lines([f"{name}: ${{{{ secrets.{name} }}}}" for name in secrets], 10)
    checks = _lines([f'[ -n "${name}" ] || missing="$missing {name}"' for name in secrets], 10)
    return f"""\
  check:
    runs-on: ubuntu-latest
    outputs:
      ready: ${{{{ steps.secrets.outputs.ready }}}}
    steps:
      - id: secrets
        name: Check the deploy secrets
        env:
{env}
        run: |
          missing=""
{checks}
          if [ -n "$missing" ]; then
            echo "::warning title=Not deployed yet::Add these repository secrets (Settings > Secrets and variables > Actions), then run this workflow again:$missing"
            echo "ready=false" >> "$GITHUB_OUTPUT"
          else
            echo "ready=true" >> "$GITHUB_OUTPUT"
          fi

"""


_REACT_BUILD = f"""\
      - uses: {SETUP_NODE}
        with:
          node-version: {NODE_VERSION}
      - name: Install
        run: npm install --no-audit --no-fund
      # Built without type-checking, like the builder's preview runs it.
      - name: Build
        run: npx vite build --base "$BASE_PATH" --outDir {OUT_DIR}
      - name: Serve unknown paths with the app
        run: cp {OUT_DIR}/index.html {OUT_DIR}/404.html
"""

_STATIC_BUILD = f"""\
      - name: Collect the site's files
        run: |
          mkdir {OUT_DIR}
          find . -mindepth 1 -maxdepth 1 ! -name '.*' ! -name {OUT_DIR} -exec cp -R {{}} {OUT_DIR}/ \\;
"""

_PAGES_JOB = f"""\
  deploy:
    runs-on: ubuntu-latest
    environment:
      name: github-pages
      url: ${{{{{{{{ steps.deployment.outputs.page_url }}}}}}}}
    steps:
{{build}}\
      - uses: {CONFIGURE_PAGES}
      - uses: {UPLOAD_PAGES_ARTIFACT}
        with:
          path: {OUT_DIR}
      - id: deployment
        uses: {DEPLOY_PAGES}
"""

_DEPLOY_JOB = """\
  deploy:
    needs: check
    if: needs.check.outputs.ready == 'true'
    runs-on: ubuntu-latest
    steps:
{build}\
"""

_VERCEL = f"""\
      - id: project
        name: Find or create the Vercel project
        env:
          VERCEL_TOKEN: ${{{{ secrets.VERCEL_TOKEN }}}}
        run: |
          api=https://api.vercel.com
          auth="Authorization: Bearer $VERCEL_TOKEN"
          res=$(curl -sS -H "$auth" "$api/v9/projects/$PROJECT_NAME")
          if [ -z "$(echo "$res" | jq -r '.id // empty')" ]; then
            body=$(jq -n --arg name "$PROJECT_NAME" '{{name: $name, framework: null}}')
            res=$(curl -sS -X POST -H "$auth" -H "Content-Type: application/json" -d "$body" "$api/v11/projects")
          fi
          id=$(echo "$res" | jq -r '.id // empty')
          if [ -z "$id" ]; then
            echo "::error title=Vercel project::$(echo "$res" | jq -r '.error.message // "Vercel did not create the project"')"
            exit 1
          fi
          echo "id=$id" >> "$GITHUB_OUTPUT"
          echo "org=$(echo "$res" | jq -r '.accountId')" >> "$GITHUB_OUTPUT"
      - name: Deploy to Vercel
        env:
          VERCEL_TOKEN: ${{{{ secrets.VERCEL_TOKEN }}}}
          VERCEL_ORG_ID: ${{{{ steps.project.outputs.org }}}}
          VERCEL_PROJECT_ID: ${{{{ steps.project.outputs.id }}}}
        run: npx --yes {VERCEL_CLI} deploy {OUT_DIR} --prod --yes --token "$VERCEL_TOKEN"
"""

_CLOUDFLARE = f"""\
      - name: Find or create the Cloudflare Pages project
        env:
          CLOUDFLARE_API_TOKEN: ${{{{ secrets.CLOUDFLARE_API_TOKEN }}}}
          CLOUDFLARE_ACCOUNT_ID: ${{{{ secrets.CLOUDFLARE_ACCOUNT_ID }}}}
        run: |
          api="https://api.cloudflare.com/client/v4/accounts/$CLOUDFLARE_ACCOUNT_ID/pages/projects"
          auth="Authorization: Bearer $CLOUDFLARE_API_TOKEN"
          if ! curl -sSf -o /dev/null -H "$auth" "$api/$PROJECT_NAME"; then
            body=$(jq -n --arg name "$PROJECT_NAME" --arg branch "$BRANCH" '{{name: $name, production_branch: $branch}}')
            res=$(curl -sS -X POST -H "$auth" -H "Content-Type: application/json" -d "$body" "$api")
            if [ "$(echo "$res" | jq -r '.success')" != "true" ]; then
              echo "::error title=Cloudflare Pages project::$(echo "$res" | jq -r '.errors[0].message // "Cloudflare did not create the project"')"
              exit 1
            fi
          fi
      - name: Deploy to Cloudflare Pages
        uses: {WRANGLER_ACTION}
        with:
          apiToken: ${{{{ secrets.CLOUDFLARE_API_TOKEN }}}}
          accountId: ${{{{ secrets.CLOUDFLARE_ACCOUNT_ID }}}}
          command: pages deploy {OUT_DIR} --project-name=${{{{ env.PROJECT_NAME }}}} --branch=${{{{ env.BRANCH }}}}
"""

_NETLIFY = f"""\
      - id: site
        name: Find or create the Netlify site
        env:
          NETLIFY_AUTH_TOKEN: ${{{{ secrets.NETLIFY_AUTH_TOKEN }}}}
        run: |
          api=https://api.netlify.com/api/v1
          auth="Authorization: Bearer $NETLIFY_AUTH_TOKEN"
          res=$(curl -sS -H "$auth" "$api/sites/$PROJECT_NAME.netlify.app")
          if [ -z "$(echo "$res" | jq -r '.id // empty')" ]; then
            body=$(jq -n --arg name "$PROJECT_NAME" '{{name: $name}}')
            res=$(curl -sS -X POST -H "$auth" -H "Content-Type: application/json" -d "$body" "$api/sites")
          fi
          id=$(echo "$res" | jq -r '.id // empty')
          if [ -z "$id" ]; then
            echo "::error title=Netlify site::$(echo "$res" | jq -r '.message // "Netlify did not create the site"')"
            exit 1
          fi
          echo "id=$id" >> "$GITHUB_OUTPUT"
      - name: Deploy to Netlify
        env:
          NETLIFY_AUTH_TOKEN: ${{{{ secrets.NETLIFY_AUTH_TOKEN }}}}
          NETLIFY_SITE_ID: ${{{{ steps.site.outputs.id }}}}
        run: npx --yes {NETLIFY_CLI} deploy --prod --dir {OUT_DIR} --no-build
"""

_DEPLOY_STEPS: dict[PublishTarget, str] = {
    PublishTarget.VERCEL: _VERCEL,
    PublishTarget.CLOUDFLARE_PAGES: _CLOUDFLARE,
    PublishTarget.NETLIFY: _NETLIFY,
}
