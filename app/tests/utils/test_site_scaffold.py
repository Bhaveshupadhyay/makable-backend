import json
from uuid import UUID

from app.utils.site_scaffold import package_name, repo_name_for, scaffold_files

PROJECT = UUID("66666666-6666-4666-8666-666666666666")


def test_the_starter_files_build_with_vite_and_run_in_the_preview() -> None:
    files = scaffold_files("Café <Pune>", PROJECT)

    package = json.loads(files["package.json"])
    assert package["name"] == "caf-pune"
    assert package["scripts"]["build"] == "vite build"
    assert set(package["dependencies"]) == {"react", "react-dom", "lucide-react"}
    assert files["src/index.css"].startswith('@import "tailwindcss";')
    assert "@theme {" not in files["src/index.css"]
    assert '<script type="module" src="/src/main.tsx"></script>' in files["index.html"]
    assert "<title>Café &lt;Pune&gt;</title>" in files["index.html"]
    # The name goes into JSX as a string literal, never as markup.
    assert '<title>{"Caf\\u00e9 <Pune>"}</title>' in files["src/App.tsx"]
    assert json.loads(files[".makable/manifest.json"]) == {
        "format": "makable-site",
        "version": 2,
        "projectId": str(PROJECT),
    }


def test_names_become_repo_and_package_names() -> None:
    assert repo_name_for("My Bakery!") == "my-bakery"
    assert repo_name_for("...") == "site"
    assert repo_name_for("a.b_c") == "a.b_c"
    assert package_name("Ünïcode only") == "n-code-only"
