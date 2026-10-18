import pytest

from app.utils.syntax_check import (
    css_braces_balanced,
    locate_syntax_error,
    locate_unbalanced_brace,
    script_language,
    script_syntax_error,
)


@pytest.mark.parametrize(
    ("path", "code", "ok"),
    [
        ("src/Hero.tsx", "export function Hero() { return <a className='x'>{name}</a> }", True),
        ("src/Hero.tsx", "export function Hero() { return <a className='x'>{name}</a }", False),
        ("src/types.ts", "export type P = { name: string }\nconst n = <number>value", True),
        ("src/types.ts", "export type P = { name: string ", False),
        ("main.js", "const el = h('div', {}, [a, b])", True),
        ("main.js", "const el = h('div', {}, [a, b)", False),
    ],
)
def test_finds_syntax_errors_in_scripts(path: str, code: str, ok: bool) -> None:
    language = script_language(path)
    assert language is not None

    error = script_syntax_error(code, language)

    assert (error is None) is ok
    if error:
        assert error.startswith("syntax error (line 1)")


def test_only_scripts_are_parsed() -> None:
    assert script_language("src/index.css") is None
    assert script_language("index.html") is None


def test_css_braces_ignore_comments_and_strings() -> None:
    assert css_braces_balanced('a { content: "}"; } /* { */')
    assert not css_braces_balanced("a { color: red; ")
    assert not css_braces_balanced("} a {")


def test_css_brace_errors_say_where() -> None:
    assert locate_unbalanced_brace('a { content: "}"; }') is None
    assert locate_unbalanced_brace("a { color: red; }\n} b { }") == ("unbalanced braces: a } that closes nothing", 18)
    unclosed = "@media (x) { a { color: red; }\nb { color: blue;\n"
    never_closed = unclosed.index("b {") + 2
    assert locate_unbalanced_brace(unclosed) == ("unbalanced braces: a { that is never closed", never_closed)
    assert locate_unbalanced_brace("/* } */ a { }") is None


def test_script_syntax_errors_say_where() -> None:
    code = 'const a = 1,b = () => e.jsx("p", {children: "x"}));'
    found = locate_syntax_error(code, script_language("a.js"))  # type: ignore[arg-type]

    assert found == ("syntax error (line 1): unexpected code", code.index("));") + 1)  # The stray ")".
    # Offsets count characters, not UTF-8 bytes.
    accented = 'const ok = 1;\nconst é = "ü";\nfoo(;\n'
    found = locate_syntax_error(accented, script_language("a.js"))  # type: ignore[arg-type]
    assert found == ("syntax error (line 3): missing )", accented.index("foo(;") + 4)


@pytest.mark.parametrize("path", ["a.tsx", "a.jsx"])
@pytest.mark.parametrize(
    "text",
    ["Visit & Order", "Lens & Light", "R&D", "A && B", "&", "5 &lt; 6 & 7", "Terms &\n  Conditions", "a & {x} & b"],
)
def test_a_bare_ampersand_in_jsx_text_parses(path: str, text: str) -> None:
    # esbuild, TypeScript and the preview take it as text; tree-sitter's grammars want an entity there.
    code = f"export const a = <p>{text}</p>\nexport const b = <>{text}</>\n"

    assert locate_syntax_error(code, script_language(path)) is None  # type: ignore[arg-type]


def test_a_file_full_of_bare_ampersands_parses() -> None:
    items = "".join(f"    <li>Item {i}: R & D</li>\n" for i in range(400))
    code = f"export const A = () => (\n  <ul>\n{items}  </ul>\n)\n"

    assert locate_syntax_error(code, script_language("a.tsx")) is None  # type: ignore[arg-type]


def test_errors_after_a_bare_ampersand_are_still_found_where_they_are() -> None:
    code = "const a = <h2>Visit & Order</h2>\nconst b = (1;\n"

    found = locate_syntax_error(code, script_language("a.tsx"))  # type: ignore[arg-type]

    assert found == ("syntax error (line 2): missing )", code.index("(1;") + 2)
    assert locate_syntax_error("const a = <h2>Visit & Order</h2\n", script_language("a.tsx")) is not None  # type: ignore[arg-type]
