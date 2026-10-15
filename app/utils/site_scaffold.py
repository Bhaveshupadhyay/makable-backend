"""The files a new site starts from: a React + Vite + TypeScript + Tailwind v4 project that works both in the
builder's preview (Sandpack runs `src/**` with the dependencies from package.json and compiles Tailwind in the
browser) and in the deploy workflow (`npm install` + `vite build`). It's a skeleton, not a design: the AI writes the
site itself.

The preview's rules, which the AI's prompts repeat:
- The entry is `src/main.tsx`; the preview makes its own `index.html`, so the page's `<title>` is set from `App`.
- CSS starts with `@import "tailwindcss";` and has no `@theme`/`@apply` or other Tailwind-only directives (the
  preview strips the import and runs Tailwind's browser build). Colours and fonts are plain CSS variables.
- Dependencies are fetched from a CDN by the preview: keep them few (react, react-dom, lucide-react).
"""

import json
import re
from uuid import UUID

from app.constants.site import MANIFEST_FORMAT, MANIFEST_VERSION

# The versions makable's earlier site templates were built and tested with, in the preview and the deploy.
DEPENDENCIES = {"lucide-react": "^1.52.0", "react": "^19.2.8", "react-dom": "^19.2.8"}
DEV_DEPENDENCIES = {
    "@tailwindcss/vite": "^4.3.3",
    "@types/react": "^19.2.18",
    "@types/react-dom": "^19.2.7",
    "@vitejs/plugin-react": "^6.1.1",
    "tailwindcss": "^4.3.3",
    "typescript": "~6.0.2",
    "vite": "^8.3.0",
}

VITE_CONFIG = """\
import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

export default defineConfig({
  plugins: [react(), tailwindcss()],
})
"""

TSCONFIG = """\
{
  "compilerOptions": {
    "target": "es2023",
    "lib": ["ES2023", "DOM"],
    "module": "esnext",
    "types": ["vite/client"],
    "skipLibCheck": true,
    "moduleResolution": "bundler",
    "verbatimModuleSyntax": true,
    "moduleDetection": "force",
    "noEmit": true,
    "jsx": "react-jsx",
    "strict": true
  },
  "include": ["src"]
}
"""

MAIN_TSX = """\
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { App } from './App'
import './index.css'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
"""

INDEX_CSS = """\
@import "tailwindcss";

/*
 * Plain CSS variables (not @theme): they work both in the Vite build and in the builder's preview, where Tailwind
 * runs in the browser. Use them from Tailwind as `bg-(--bg)`, `text-(--fg)` and so on.
 */
:root {
  --bg: #ffffff;
  --fg: #0a0a0a;
  --muted: #6b7280;
  --accent: #2563eb;
  --font: ui-sans-serif, system-ui, sans-serif;
}

body {
  margin: 0;
  background: var(--bg);
  color: var(--fg);
  font-family: var(--font);
}
"""

GITIGNORE = "node_modules\ndist\n"


def scaffold_files(name: str, project_id: UUID) -> dict[str, str]:
    """A new site's files, named `name`."""
    package = {
        "name": package_name(name),
        "private": True,
        "version": "0.0.0",
        "type": "module",
        # No `tsc` before the build: the preview doesn't type-check either, so a type error never stops a deploy.
        "scripts": {"dev": "vite", "build": "vite build", "preview": "vite preview"},
        "dependencies": DEPENDENCIES,
        "devDependencies": DEV_DEPENDENCIES,
    }
    title = json.dumps(name)
    return {
        ".gitignore": GITIGNORE,
        "README.md": f"# {name}\n\nBuilt with [makable](https://makable.tech).\n",
        "index.html": _index_html(name),
        "package.json": json.dumps(package, indent=2) + "\n",
        "tsconfig.json": TSCONFIG,
        "vite.config.ts": VITE_CONFIG,
        "src/main.tsx": MAIN_TSX,
        "src/index.css": INDEX_CSS,
        "src/App.tsx": (
            "export function App() {\n"
            "  return (\n"
            '    <main className="flex min-h-screen items-center justify-center p-8">\n'
            f"      <title>{{{title}}}</title>\n"
            f'      <h1 className="text-4xl font-bold">{{{title}}}</h1>\n'
            "    </main>\n"
            "  )\n"
            "}\n"
        ),
        **manifest_file(project_id),
    }


def manifest_file(project_id: UUID) -> dict[str, str]:
    """The manifest that marks a repo as this site's."""
    manifest = {"format": MANIFEST_FORMAT, "version": MANIFEST_VERSION, "projectId": str(project_id)}
    return {".makable/manifest.json": json.dumps(manifest, indent=2) + "\n"}


def package_name(name: str) -> str:
    """An npm package name for the site (lowercase letters, digits and dashes)."""
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:60].rstrip("-") or "site"


def repo_name_for(name: str) -> str:
    """The repo name a site called `name` gets: what GitHub would make of it, in lowercase."""
    return re.sub(r"[^a-z0-9._-]+", "-", name.lower()).strip("-.")[:100].rstrip("-.") or "site"


def _index_html(name: str) -> str:
    title = name.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return (
        "<!doctype html>\n"
        '<html lang="en">\n'
        "  <head>\n"
        '    <meta charset="UTF-8" />\n'
        '    <meta name="viewport" content="width=device-width, initial-scale=1.0" />\n'
        f"    <title>{title}</title>\n"
        "  </head>\n"
        "  <body>\n"
        '    <div id="root"></div>\n'
        '    <script type="module" src="/src/main.tsx"></script>\n'
        "  </body>\n"
        "</html>\n"
    )
