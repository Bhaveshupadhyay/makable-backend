import pytest

from app.agents.create_site.tools.checks import check_site, exported_names, lucide_icons
from app.agents.create_site.tools.code import extract_code
from app.agents.create_site.tools.names import suggest_name
from app.agents.create_site.tools.plan import parse_plan


def test_a_name_is_suggested_from_the_prompt() -> None:
    assert suggest_name('Make a website for my bakery called "Ada\'s Buns"') == "Ada's Buns"
    assert suggest_name("Create a landing page for a dog walking business in Pune") == "Dog Walking Business Pune"
    assert suggest_name("make a website") == "My site"


def test_the_file_is_the_first_fenced_block() -> None:
    assert extract_code("Here you go:\n```tsx\nexport const a = 1\n```\nDone.") == "export const a = 1\n"
    # A longer fence can hold shorter ones.
    assert extract_code("````md\n```\ninner\n```\n````") == "```\ninner\n```\n"
    with pytest.raises(ValueError, match="no code block"):
        extract_code("export const a = 1")
    with pytest.raises(ValueError, match="isn't closed"):
        extract_code("```tsx\nexport const a = 1\n")


def test_a_plan_needs_app_and_files_under_src() -> None:
    plan = parse_plan(
        '{"summary": "A bakery.", "files": [{"path": "src/App.tsx", "purpose": "page"}, '
        '{"path": "src/index.css", "purpose": "styles"}]}'
    )
    assert [f.path for f in plan.files] == ["src/App.tsx", "src/index.css"]
    for files, error in (
        ('[{"path": "src/Hero.tsx", "purpose": "x"}]', "src/App.tsx"),
        ('[{"path": "src/App.tsx", "purpose": "x"}, {"path": "src/main.tsx", "purpose": "x"}]', "under src/"),
        ('[{"path": "src/App.tsx", "purpose": "x"}, {"path": "package.json", "purpose": "x"}]', "under src/"),
        ('[{"path": "src/App.tsx", "purpose": "x"}, {"path": "src/App.tsx", "purpose": "y"}]', "more than once"),
    ):
        with pytest.raises(ValueError, match=error):
            parse_plan(f'{{"summary": "x", "files": {files}}}')


def test_the_checks_find_what_would_break_the_preview() -> None:
    files = {
        "src/main.tsx": "import { App } from './App'\nimport './index.css'\n",
        "src/index.css": '@import "tailwindcss";\n',
        "src/App.tsx": (
            "import { Hero } from './components/Hero'\n"
            "import Footer from './components/Footer'\n"
            "import { Menu } from './components/Menu'\n"
            "import { motion } from 'framer-motion'\n"
            "import { Instagram, Mail } from 'lucide-react'\n"
            "export default function Page() { return <main><Hero /></main> }\n"
        ),
        "src/components/Hero.tsx": "export function Hero() { return <section /> }\n",
        "src/components/Footer.tsx": "export function Footer() { return <footer /> }\n",
        "src/components/Broken.tsx": "export function Broken() { return <div> }\n",
    }

    problems = check_site(files, ["src/App.tsx", "src/components/Hero.tsx", "src/components/Broken.tsx"])

    app = problems["src/App.tsx"]
    assert any("imports Footer as the default export" in p for p in app)
    assert any('imports "./components/Menu", but there\'s no such file' in p for p in app)
    assert any('the package "framer-motion"' in p for p in app)
    assert any("no icon named Instagram" in p for p in app)
    assert not any("Mail" in p for p in app)
    assert any("must export a component named App" in p for p in app)
    assert "src/components/Hero.tsx" not in problems
    assert "syntax error" in problems["src/components/Broken.tsx"][0]


def test_exports_and_icons() -> None:
    code = "export function A() {}\nexport const B = 1\nconst C = 2\nexport { C as D }\nexport default A\n"
    assert exported_names(code) == {"A", "B", "D", "default"}
    assert {"Mail", "Phone", "MapPin", "Cake", "Home"} <= lucide_icons()
    assert not {"Instagram", "Github", "Facebook"} & lucide_icons()
