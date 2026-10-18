import pytest

from app.agents.create_site.nodes.write import _line_at
from app.agents.create_site.tools.brief import BriefReady, answered, brief_block, parse_brief_reply
from app.agents.create_site.tools.checks import check_site, exported_names, lucide_icons
from app.agents.create_site.tools.code import extract_code
from app.agents.create_site.tools.names import NameChat, NameGiven, mentions, parse_name_reply, same_name, suggest_name
from app.agents.create_site.tools.plan import parse_plan
from app.agents.thread import QuestionAnswer
from app.constants.ai_create import REJECTED_LINE_CHARS
from app.schemas.ai_edit import ModelQuestion


def test_a_name_is_suggested_from_the_prompt() -> None:
    assert suggest_name('Make a website for my bakery called "Ada\'s Buns"') == "Ada's Buns"
    assert suggest_name("Create a landing page for a dog walking business in Pune") == "Dog Walking Business Pune"
    assert suggest_name("make a website") == "My site"


def test_a_suggested_name_is_kept_short() -> None:
    long_words = "Build a landing page for Northwestern-Pacific-Environmental Consulting Group Sustainability"
    assert suggest_name(long_words) == "Northwestern-Pacific-Environmental"
    assert len(suggest_name("A site for " + "Supercalifragilistic" * 5)) == 40


def test_the_models_reading_of_a_name_answer_is_a_name_or_a_reply() -> None:
    assert parse_name_reply('{"name": " Sunrise Bakery "}') == NameGiven(name="Sunrise Bakery")
    chat = parse_name_reply('Sure:\n{"reply": "Hello! What should we call it?", "suggestions": ["Sunrise"]}')
    assert chat == NameChat(reply="Hello! What should we call it?", suggestions=["Sunrise"])
    assert parse_name_reply('{"reply": "What should we call it?"}') == NameChat(reply="What should we call it?")
    # An empty name isn't one; then it must be a reply.
    with pytest.raises(ValueError):
        parse_name_reply('{"name": ""}')
    with pytest.raises(ValueError):
        parse_name_reply('{"reply": "Pick one", "suggestions": ["a", "b", "c", "d"]}')
    with pytest.raises(ValueError):
        parse_name_reply("Sunrise Bakery")


def test_names_are_compared_without_case_or_spacing() -> None:
    assert same_name("Ada's  Bakery", " ada's bakery")
    assert not same_name("Ada's Bakery", "Ada's")
    assert mentions("call it   SUNRISE bakery please", "Sunrise Bakery")
    assert not mentions("you pick", "Sunrise Bakery")


def test_the_models_reply_about_the_site_is_a_question_or_ready() -> None:
    asked = parse_brief_reply('{"ask": {"question": "What kind of site is it?", "options": ["A shop", "A blog"]}}')
    assert isinstance(asked, ModelQuestion)
    assert (asked.ask.question, asked.ask.options) == ("What kind of site is it?", ["A shop", "A blog"])
    assert parse_brief_reply('Ready:\n{"ready": true}') == BriefReady(ready=True)
    named = parse_brief_reply('{"ready": true, "names": [" Crumb ", "Ada\'s Cakes"]}')
    assert named == BriefReady(ready=True, names=["Crumb", "Ada's Cakes"])
    too_many = '{"ready": true, "names": ["a", "b", "c", "d"]}'
    for bad in ('{"ready": false}', too_many, '{"ask": {"question": ""}}', "no json"):
        with pytest.raises(ValueError):
            parse_brief_reply(bad)


def test_only_answers_count_and_go_to_the_plan() -> None:
    brief = [
        QuestionAnswer(question="What kind?", answer="what do you mean?", chat=True),
        QuestionAnswer(question="A shop or a blog?", answer="A shop"),
    ]
    assert answered(brief) == 1
    assert brief_block(brief) == "<user_answers>\nQ: A shop or a blog?\nA: A shop\n</user_answers>\n"
    assert brief_block([]) == ""


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


def test_the_site_must_show_its_name_and_have_it_as_its_title() -> None:
    header = 'export function Header() { return <header><a href="#top">My Bakery</a></header> }\n'
    app = "export function App() { return <main><title>My Bakery</title></main> }\n"
    files = {"src/App.tsx": app, "src/components/Header.tsx": header}
    written = ["src/components/Header.tsx", "src/App.tsx"]

    # Its case and spacing, and HTML entities, don't matter.
    assert check_site(files, written, "my  bakery") == {}
    assert check_site({**files, "src/App.tsx": app.replace("My Bakery", "Crumb &amp; Co")}, written, "Crumb & Co") == {
        "src/components/Header.tsx": [
            'the site is called "Crumb & Co", but the page never shows that name: show it as the site\'s brand in the '
            "header (and in the footer), exactly as written, as text in the code"
        ]
    }

    problems = check_site({**files, "src/App.tsx": app.replace("<title>My Bakery</title>", "")}, written, "My Bakery")
    assert problems == {
        "src/App.tsx": [
            'the site is called "My Bakery", and the page\'s title must have that name: put <title>My Bakery</title> '
            "inside App, exactly as written"
        ]
    }

    # A name from the description isn't the one the user chose.
    problems = check_site(files, written, "Sunrise")
    assert [p.split(",")[0] for p in problems["src/App.tsx"]] == ['the site is called "Sunrise"']
    assert "src/components/Header.tsx" in problems
    # Without a header file, the page is told to show it.
    assert "never shows that name" in check_site({"src/App.tsx": app}, ["src/App.tsx"], "Sunrise")["src/App.tsx"][1]
    assert check_site(files, written) == {}


def test_exports_and_icons() -> None:
    code = "export function A() {}\nexport const B = 1\nconst C = 2\nexport { C as D }\nexport default A\n"
    assert exported_names(code) == {"A", "B", "D", "default"}
    assert {"Mail", "Phone", "MapPin", "Cake", "Home"} <= lucide_icons()
    assert not {"Instagram", "Github", "Facebook"} & lucide_icons()


def test_a_rejected_file_quotes_the_line_around_its_error() -> None:
    assert _line_at("const a = 1\nconst b = (1;\n", 24) == "const b = (1;"
    # At the end of the file (a missing `}`): the last line with text.
    code = "export function A() {\n  return <p>hi</p>\n\n"
    assert _line_at(code, len(code)) == "return <p>hi</p>"
    # A long line is cut around the error, not after its start.
    long = "const x = [" + ", ".join(str(i) for i in range(150)) + ", (];"
    quoted = _line_at(long, long.index("(]"))
    assert "(];" in quoted
    assert len(quoted) <= REJECTED_LINE_CHARS
