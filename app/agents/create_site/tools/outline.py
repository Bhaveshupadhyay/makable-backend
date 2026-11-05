"""What a file the model wrote looks like to the files written after it when it's too long to show whole: what they
need to use it right (its types, its components' props, the shape of its data), without its markup."""

import re

from tree_sitter import Node, Parser

from app.constants.ai_create import (
    MAX_OUTLINE_CHARS,
    MAX_OUTLINE_ENTRY_CHARS,
    OUTLINE_OBJECT_KEYS,
    OUTLINE_VALUE_CHARS,
)
from app.utils.syntax_check import script_language

# Shown whole, exported or not: they're the shapes other files use (a component's props are usually a local interface).
_TYPES = frozenset({"interface_declaration", "type_alias_declaration", "enum_declaration"})
_FUNCTIONS = frozenset({"function_declaration", "generator_function_declaration"})
_CLASSES = frozenset({"class_declaration", "abstract_class_declaration"})
_VARIABLES = frozenset({"lexical_declaration", "variable_declaration"})
_FUNCTION_VALUES = frozenset({"arrow_function", "function_expression", "function", "generator_function"})
_BLOCKS = frozenset({"statement_block", "class_body"})
_STRINGS = frozenset({"string", "template_string"})
# A stylesheet's imports, at-rules and selectors (also the ones nested in `@media`), and its custom properties
# (`--accent: #dc2626;`), which the components use in classes like `bg-(--accent)`.
_CSS_OUTLINE = re.compile(r"^\s*(?:@[\w-]+|--[\w-]+\s*:|[^\s{}][^{}]*\{)")


def outline(path: str, code: str) -> str:
    """The outline of a script or a stylesheet, at most about `MAX_OUTLINE_CHARS`.

    A script's: its types whole, its exported functions, components and classes as their signatures (`{ … }` for the
    body), and its exported data with each list's first item (`/* …N more */` for the rest) and long values cut.
    Imports, markup and what the file keeps to itself are left out.
    """
    language = script_language(path)
    if language is None:
        lines = [line.rstrip() for line in code.split("\n") if _CSS_OUTLINE.search(line)]
        return _fit(lines, "lines")
    return _fit(_script_outline(Parser(language).parse(code.encode()).root_node), "declarations")


def _fit(entries: list[str], noun: str) -> str:
    """The entries that fit in `MAX_OUTLINE_CHARS`, in order, each cut after `MAX_OUTLINE_ENTRY_CHARS` first, so a
    long one doesn't push out what comes after it. The ones that don't fit are counted at the end."""
    kept: list[str] = []
    left = MAX_OUTLINE_CHARS
    dropped = 0
    for entry in map(_shorten, entries):
        if len(entry) + 1 > left:
            dropped += 1
            continue
        left -= len(entry) + 1
        kept.append(entry)
    if dropped:
        kept.append(f"⟪cut: {dropped} more {noun}⟫")
    return "\n".join(kept)


def _shorten(entry: str) -> str:
    """An entry cut after `MAX_OUTLINE_ENTRY_CHARS`, at a line when it has several (a long interface keeps its first
    fields)."""
    if len(entry) <= MAX_OUTLINE_ENTRY_CHARS:
        return entry
    head = entry[:MAX_OUTLINE_ENTRY_CHARS]
    if "\n" in head:
        head = head[: head.rindex("\n")]
    marker = f"⟪cut: {len(entry) - len(head)} more characters⟫"
    return f"{head}\n{marker}" if "\n" in entry else f"{head} {marker}"


def _script_outline(root: Node) -> list[str]:
    """A script's outline, one entry per declaration (a type can take several lines)."""
    exported = _exported_by_name(root)
    lines: list[str] = []
    for node in root.named_children:
        if node.type == "export_statement":
            lines += _export(node)
        elif node.type in _TYPES:
            lines.append(_text(node))
        elif _declared_names(node) & exported:
            # Declared here and exported further down (`export default Hero`, `export { Hero }`).
            lines += _declaration(node, "")
    return lines


def _exported_by_name(root: Node) -> set[str]:
    """The local names the file exports in `export default <name>` or `export { <name> }` (not re-exports)."""
    names: set[str] = set()
    for node in root.named_children:
        if node.type != "export_statement" or node.child_by_field_name("source") is not None:
            continue
        for child in node.named_children:
            if child.type == "identifier":
                names.add(_text(child))
            elif child.type == "export_clause":
                for spec in child.named_children:
                    name = spec.child_by_field_name("name")
                    if name is not None:
                        names.add(_text(name))
    return names


def _export(node: Node) -> list[str]:
    """An `export` statement: its declaration's outline, `export default <value>;`, or the statement on one line."""
    prefix = "export default " if any(child.type == "default" for child in node.children) else "export "
    for child in node.named_children:
        if child.type in _TYPES | _FUNCTIONS | _CLASSES | _VARIABLES:
            return _declaration(child, prefix)
        if child.type not in {"export_clause", "string", "comment"} and prefix == "export default ":
            return [f"export default {_value(child)};"]
    return [_flat(node)]


def _declaration(node: Node, prefix: str) -> list[str]:
    """A declaration after `prefix` (`export `, `export default ` or nothing): a type whole, a function or class as its
    signature, and one entry per variable it declares."""
    if node.type in _TYPES:
        return [prefix + _text(node)]
    if node.type in _VARIABLES:
        kind = _text(node.children[0])
        return [f"{prefix}{kind} {_declarator(d)}" for d in node.named_children if d.type == "variable_declarator"]
    return [prefix + _signature(node)]


def _declared_names(node: Node) -> set[str]:
    """The names a top-level function, class or variable declaration declares (none for other statements)."""
    if node.type in _FUNCTIONS | _CLASSES:
        name = node.child_by_field_name("name")
        return {_text(name)} if name is not None else set()
    if node.type in _VARIABLES:
        declared = (d.child_by_field_name("name") for d in node.named_children if d.type == "variable_declarator")
        return {_text(name) for name in declared if name is not None}
    return set()


def _declarator(node: Node) -> str:
    """`name: Type = <value, shortened>`."""
    value = node.child_by_field_name("value")
    if value is None:
        return _flat(node)
    return f"{_before(node, value)} {_value(value)}"


def _signature(node: Node) -> str:
    """A function's or class's text up to its body: `function Hero({ title }: HeroProps) { … }`."""
    body = node.child_by_field_name("body")
    if body is None:
        return _cut(_flat(node))
    head = _before(node, body)
    return f"{head} {{ … }}" if body.type in _BLOCKS else f"{head} …"


def _value(node: Node) -> str:
    """A value in data, shortened: a list shows its first item, functions their signature, long values are cut."""
    if node.type in _FUNCTION_VALUES:
        return _signature(node)
    if node.type == "array":
        items = [child for child in node.named_children if child.type != "comment"]
        if not items:
            return "[]"
        more = f", /* …{len(items) - 1} more */" if len(items) > 1 else ""
        return f"[{_value(items[0])}{more}]"
    if node.type == "object":
        entries = [child for child in node.named_children if child.type != "comment"]
        shown = [_entry(entry) for entry in entries[:OUTLINE_OBJECT_KEYS]]
        if len(entries) > OUTLINE_OBJECT_KEYS:
            shown.append(f"/* …{len(entries) - OUTLINE_OBJECT_KEYS} more */")
        return f"{{ {', '.join(shown)} }}" if shown else "{}"
    if node.type in {"as_expression", "satisfies_expression"} and node.named_children:
        inner = node.named_children[0]
        return f"{_value(inner)} {_after(node, inner)}"
    if node.type == "call_expression":
        callee, arguments = node.child_by_field_name("function"), node.child_by_field_name("arguments")
        if callee is not None and arguments is not None and arguments.type == "arguments":
            args = ", ".join(_value(arg) for arg in arguments.named_children if arg.type != "comment")
            return f"{_cut(_flat(callee))}({args})"
    text = _flat(node)
    return _cut(text, text[-1]) if node.type in _STRINGS else _cut(text)


def _entry(node: Node) -> str:
    """One entry of an object in data: `key: <value, shortened>`, a method's signature, or a shorthand or spread."""
    if node.type == "pair":
        key, value = node.child_by_field_name("key"), node.child_by_field_name("value")
        if key is not None and value is not None:
            return f"{_flat(key)}: {_value(value)}"
    if node.type == "method_definition":
        return _signature(node)
    return _cut(_flat(node))


def _cut(text: str, closing: str = "") -> str:
    """`text` up to `OUTLINE_VALUE_CHARS`, marked `…` where it's cut (a string keeps its closing quote)."""
    if len(text) <= OUTLINE_VALUE_CHARS:
        return text
    return f"{text[: OUTLINE_VALUE_CHARS - 1 - len(closing)].rstrip()}…{closing}"


def _flat(node: Node) -> str:
    """A node's code on one line (its runs of whitespace as one space)."""
    return _one_line(node.text or b"")


def _before(node: Node, child: Node) -> str:
    """`node`'s code up to where `child` starts, on one line."""
    return _one_line((node.text or b"")[: child.start_byte - node.start_byte])


def _after(node: Node, child: Node) -> str:
    """`node`'s code after where `child` ends, on one line."""
    return _one_line((node.text or b"")[child.end_byte - node.start_byte :])


def _one_line(raw: bytes) -> str:
    return " ".join(raw.decode(errors="replace").split())


def _text(node: Node) -> str:
    return (node.text or b"").decode(errors="replace")
