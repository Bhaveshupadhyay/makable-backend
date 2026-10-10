"""Cheap checks that an edited source file still parses. They catch the usual model mistakes (a dropped
closing tag or brace) before the edit reaches the preview. They don't type-check: the preview doesn't either."""

import re

import tree_sitter_javascript
import tree_sitter_typescript
from tree_sitter import Language, Node, Parser

_JAVASCRIPT = Language(tree_sitter_javascript.language())  # includes JSX
_TYPESCRIPT = Language(tree_sitter_typescript.language_typescript())
_TSX = Language(tree_sitter_typescript.language_tsx())

_CSS_NOISE = re.compile(r"/\*.*?\*/|\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'", re.DOTALL)


def script_language(path: str) -> Language | None:
    """The grammar for a script path, or None when the file isn't a script."""
    if path.endswith((".ts", ".mts")):
        return _TYPESCRIPT
    if path.endswith((".tsx", ".mtsx")):
        return _TSX
    if path.endswith((".js", ".mjs", ".jsx", ".mjsx")):
        return _JAVASCRIPT
    return None


def script_syntax_error(code: str, language: Language) -> str | None:
    """The first syntax error in a script, as `syntax error (line N)`, or None if it parses."""
    tree = Parser(language).parse(code.encode())
    node = _first_error(tree.root_node)
    if node is None:
        return None
    what = f"missing {node.type}" if node.is_missing else "unexpected code"
    return f"syntax error (line {node.start_point.row + 1}): {what}"


def _first_error(node: Node) -> Node | None:
    if node.is_error or node.is_missing:
        return node
    for child in node.children:
        if child.has_error:
            found = _first_error(child)
            if found is not None:
                return found
    return None


def css_braces_balanced(code: str) -> bool:
    """Braces in a stylesheet, ignoring comments and strings, open and close in order."""
    depth = 0
    for ch in _CSS_NOISE.sub("", code):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth < 0:
                return False
    return depth == 0
