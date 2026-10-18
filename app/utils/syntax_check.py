"""Cheap checks that an edited source file still parses. They catch the usual model mistakes (a dropped
closing tag or brace) before the edit reaches the preview. They don't type-check: the preview doesn't either."""

import re
from collections.abc import Iterator

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
    found = locate_syntax_error(code, language)
    return found[0] if found else None


def locate_syntax_error(code: str, language: Language) -> tuple[str, int] | None:
    """The first syntax error in a script and its character offset, or None if it parses."""
    encoded = bytearray(code.encode())
    parser = Parser(language)
    while True:
        root = parser.parse(bytes(encoded)).root_node
        node = _first_error(root)
        if node is None or not _bare_jsx_ampersand(node, encoded):
            break
        # The grammar wants an entity after `&` in JSX text, but a bare one is fine (`<h2>Visit & Order</h2>`):
        # esbuild, TypeScript and the preview take it as text. A letter in its place keeps the offsets. Every one
        # this parse found goes at once, so a file full of them is parsed a few times, not once per `&`.
        for error in _errors(root):
            if _bare_jsx_ampersand(error, encoded):
                encoded[error.start_byte] = ord("x")
    if node is None:
        return None
    what = f"missing {node.type}" if node.is_missing else "unexpected code"
    offset = len(encoded[: node.start_byte].decode(errors="ignore"))
    return f"syntax error (line {node.start_point.row + 1}): {what}", offset


def _bare_jsx_ampersand(node: Node, encoded: bytearray) -> bool:
    """The error is JSX text starting with an `&` that isn't an entity (tree-sitter's grammars reject those)."""
    in_jsx = node.parent is not None and node.parent.type == "jsx_element"
    return node.is_error and in_jsx and encoded[node.start_byte] == ord("&")


def _first_error(node: Node) -> Node | None:
    return next(_errors(node), None)


def _errors(node: Node) -> Iterator[Node]:
    """The syntax errors under a node, in order (not the ones inside an error)."""
    if node.is_error or node.is_missing:
        yield node
        return
    for child in node.children:
        if child.has_error or child.is_missing:
            yield from _errors(child)


def css_braces_balanced(code: str) -> bool:
    """Braces in a stylesheet, ignoring comments and strings, open and close in order."""
    return locate_unbalanced_brace(code) is None


def locate_unbalanced_brace(code: str) -> tuple[str, int] | None:
    """The first `}` with no `{` before it, or else the last `{` that is never closed, with its offset; None
    when the braces balance. Comments and strings are skipped."""
    noise = [m.span() for m in _CSS_NOISE.finditer(code)]
    opened: list[int] = []
    i = skip = 0
    while i < len(code):
        if skip < len(noise) and i >= noise[skip][0]:
            i, skip = max(i, noise[skip][1]), skip + 1
            continue
        if code[i] == "{":
            opened.append(i)
        elif code[i] == "}":
            if not opened:
                return "unbalanced braces: a } that closes nothing", i
            opened.pop()
        i += 1
    return ("unbalanced braces: a { that is never closed", opened[-1]) if opened else None
