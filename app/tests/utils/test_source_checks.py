import pytest

from app.schemas.portfolio import Portfolio
from app.tests.ai_edit_fixtures import PORTFOLIO, render_portfolio_source
from app.utils.portfolio_source import PortfolioSourceError, parse_portfolio_source
from app.utils.portfolio_source import render_portfolio_source as render
from app.utils.syntax_check import css_braces_balanced, script_language, script_syntax_error


def test_parses_the_content_files_the_spa_renders() -> None:
    react = render_portfolio_source(PORTFOLIO)
    static = "export const portfolio = " + react.split(" = ", 1)[1]

    assert parse_portfolio_source(react).profile.name == "Ada"
    assert parse_portfolio_source(static).template == "minimal"


def test_renders_the_content_file_like_the_spa() -> None:
    portfolio = Portfolio.model_validate({**PORTFOLIO, "skills": ["Café ☕", 'Say "hi"']})

    react = render(portfolio, "react")

    # Byte for byte what `renderPortfolioSource` writes (JSON.stringify keeps non-ASCII as is).
    assert react == render_portfolio_source({**PORTFOLIO, "skills": ["Café ☕", 'Say "hi"']}).replace(
        "\\u00e9 \\u2615", "é ☕"
    )
    assert "Café ☕" in react
    assert parse_portfolio_source(react) == portfolio
    static = render(portfolio, "static")
    assert static.startswith("// All copy on the site lives here.") and "import type" not in static
    assert parse_portfolio_source(static) == portfolio


@pytest.mark.parametrize(
    ("source", "error"),
    [
        ("const x = 1", "must keep"),
        ("export const portfolio = {'a': 1}", "valid JSON"),
        ('export const portfolio = {"a": NaN}', "valid JSON"),
        (render_portfolio_source({**PORTFOLIO, "skills": [""]}), "invalid at skills.0"),
        (render_portfolio_source({**PORTFOLIO, "links": {**PORTFOLIO["links"], "x": "javascript:x"}}), "links.x"),
        (render_portfolio_source({**PORTFOLIO, "skills": [1]}), "skills.0"),
    ],
)
def test_rejects_broken_or_invalid_content(source: str, error: str) -> None:
    with pytest.raises(PortfolioSourceError, match=error):
        parse_portfolio_source(source)


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
