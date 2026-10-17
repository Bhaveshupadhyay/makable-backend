"""Static checks on a new site's files, before it's committed: what would break the preview without running it. Each
problem is a sentence for the model, which gets the file back to fix it.

- Scripts must parse (tree-sitter) and CSS braces must balance.
- Relative imports must point at a file of the site, and named imports at something that file exports.
- Packages must be ones the site has (`ALLOWED_PACKAGES`), and lucide-react icons ones it exports (`lucide_icons.txt`,
  from lucide-react 1.52: version 1 has no brand icons such as Instagram).
- `src/App.tsx` must export `App`, which the starter's `src/main.tsx` renders.
- The site's name (what the user called it) must be the page's `<title>` in `src/App.tsx`, and be shown on the page
  (the brand in the header). Matched ignoring case, spacing and HTML entities.
"""

import html
import json
import posixpath
import re
from functools import cache
from pathlib import Path

from app.constants.ai_create import ALLOWED_PACKAGES
from app.utils.syntax_check import locate_syntax_error, locate_unbalanced_brace, script_language

_IMPORT = re.compile(
    r"^\s*(?:import\s+(type\s+)?(?P<clause>[\w$*{}\s,]+?)\s+from\s+|import\s+|export\s+(?:type\s+)?[*{][^;]*?from\s+)"
    r"['\"](?P<spec>[^'\"]+)['\"]",
    re.MULTILINE,
)
_EXPORT_DECLARED = re.compile(
    r"^\s*export\s+(?:declare\s+)?(?:async\s+)?(?:function\*?|const|let|var|class|interface|type|enum)\s+([\w$]+)",
    re.MULTILINE,
)
_EXPORT_LIST = re.compile(r"^\s*export\s+(?:type\s+)?\{([^}]*)\}", re.MULTILINE)
_EXPORT_DEFAULT = re.compile(r"^\s*export\s+default\b", re.MULTILINE)
_TITLE = re.compile(r"<title\b[^>]*>(.*?)</title>", re.DOTALL | re.IGNORECASE)
_HEADER_FILE = re.compile(r"header|nav", re.IGNORECASE)
_RESOLVE = ("", ".tsx", ".ts", ".jsx", ".js", ".css", "/index.tsx", "/index.ts")


@cache
def lucide_icons() -> frozenset[str]:
    lines = (Path(__file__).parent / "lucide_icons.txt").read_text().splitlines()
    return frozenset(line.strip() for line in lines if line.strip() and not line.startswith("#"))


def check_site(files: dict[str, str], paths: list[str], name: str = "") -> dict[str, list[str]]:
    """The problems of each of `paths` (the files the model wrote), checked against all of `files`; with `name`, also
    whether the site shows its name."""
    problems: dict[str, list[str]] = {}
    for path in paths:
        found = _problems(files, path)
        if found:
            problems[path] = found
    app = files.get("src/App.tsx")
    if app is not None and "App" not in exported_names(app):
        problems.setdefault("src/App.tsx", []).append(
            "it must export a component named App (`export function App() { ... }`): src/main.tsx imports { App }"
        )
    if name and app is not None:
        for path, problem in _name_problems(files, paths, name):
            problems.setdefault(path, []).append(problem)
    return problems


def exported_names(code: str) -> set[str]:
    """The names a module exports (`default` for a default export)."""
    names = set(_EXPORT_DECLARED.findall(code))
    for block in _EXPORT_LIST.findall(code):
        for item in block.split(","):
            item = item.strip().removeprefix("type ").strip()
            if item:
                names.add(item.split(" as ")[-1].strip())
    if _EXPORT_DEFAULT.search(code):
        names.add("default")
    return names


def _problems(files: dict[str, str], path: str) -> list[str]:
    code = files[path]
    language = script_language(path)
    if language is None:
        found = locate_unbalanced_brace(code) if path.endswith(".css") else None
        return [f"{found[0]} (at line {_line(code, found[1])})"] if found else []
    problems = []
    syntax = locate_syntax_error(code, language)
    if syntax:
        problems.append(f"{syntax[0]} (at line {_line(code, syntax[1])})")
    for match in _IMPORT.finditer(code):
        spec, clause, line = match.group("spec"), match.group("clause"), _line(code, match.start("spec"))
        type_only = bool(match.group(1))
        if spec.startswith("."):
            problems += _relative_import(files, path, spec, clause, line, type_only)
        elif spec == "lucide-react":
            missing = sorted(_named(clause) - lucide_icons())
            if missing:
                problems.append(
                    f"line {line}: lucide-react has no icon named {', '.join(missing)} (version 1 has no brand icons "
                    "such as Instagram or Github): use another icon, or none"
                )
        elif spec not in ALLOWED_PACKAGES:
            problems.append(
                f'line {line}: imports the package "{spec}", which the site doesn\'t have: only react, react-dom and '
                "lucide-react can be imported"
            )
    return problems


def _name_problems(files: dict[str, str], paths: list[str], name: str) -> list[tuple[str, str]]:
    """Whether the page's title is the site's name, and whether the page shows it (outside the title)."""
    wanted = _plain(name)
    app = files["src/App.tsx"]
    problems = []
    if not any(wanted in _plain(title) for title in _TITLE.findall(app)):
        problems.append(
            (
                "src/App.tsx",
                f"the site is called {json.dumps(name, ensure_ascii=False)}, and the page's title must have that name: "
                f"put <title>{name}</title> inside App, exactly as written",
            )
        )
    scripts = [p for p in paths if script_language(p) is not None and p in files]
    if not any(wanted in _plain(_TITLE.sub("", files[p])) for p in scripts):
        header = next((p for p in scripts if _HEADER_FILE.search(posixpath.basename(p))), "src/App.tsx")
        problems.append(
            (
                header,
                f"the site is called {json.dumps(name, ensure_ascii=False)}, but the page never shows that name: show "
                "it as the site's brand in the header (and in the footer), exactly as written, as text in the code",
            )
        )
    return problems


def _plain(text: str) -> str:
    """Text as it reads on the page, to find a name in code: entities and escaped quotes undone, spaces collapsed,
    without case."""
    text = html.unescape(text.replace("\\'", "'").replace('\\"', '"'))
    return " ".join(text.split()).casefold()


def _relative_import(
    files: dict[str, str], path: str, spec: str, clause: str | None, line: int, type_only: bool
) -> list[str]:
    target = _resolve(files, path, spec)
    if target is None:
        return [f'line {line}: imports "{spec}", but there\'s no such file in the site']
    if not clause or type_only or script_language(target) is None:
        return []
    exported = exported_names(files[target])
    problems = []
    default = _default(clause)
    if default and "default" not in exported:
        problems.append(
            f'line {line}: imports {default} as the default export of "{spec}", which has none: import it by name '
            f"({{ {default} }}) if {target} exports it so"
        )
    missing = sorted(_named(clause) - exported)
    if missing:
        problems.append(f'line {line}: imports {", ".join(missing)} from "{spec}", which doesn\'t export that')
    return problems


def _resolve(files: dict[str, str], path: str, spec: str) -> str | None:
    base = posixpath.normpath(posixpath.join(posixpath.dirname(path), spec))
    return next((base + ending for ending in _RESOLVE if base + ending in files), None)


def _named(clause: str | None) -> set[str]:
    """The names in `{ a, b as c, type D }` (what's imported, not the local aliases)."""
    if not clause or "{" not in clause:
        return set()
    inside = clause[clause.index("{") + 1 : clause.rindex("}")] if "}" in clause else ""
    names = set()
    for item in inside.split(","):
        item = item.strip()
        if item and not item.startswith("type "):
            names.add(item.split(" as ")[0].strip())
    return names


def _default(clause: str) -> str | None:
    head = clause.split("{")[0].split(",")[0].strip()
    return head if re.fullmatch(r"[\w$]+", head) else None


def _line(code: str, offset: int | None) -> int:
    return code[: offset or 0].count("\n") + 1
