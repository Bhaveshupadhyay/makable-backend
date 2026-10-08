import pytest

from app.utils.return_to import safe_return_to, with_query


@pytest.mark.parametrize("value", ["/", "/projects/1?tab=preview#top"])
def test_keeps_same_origin_paths(value: str) -> None:
    assert safe_return_to(value) == value


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "projects",
        "https://evil.com",
        "//evil.com",
        "/\\evil.com",
        "/\\/evil.com",
        "javascript:alert(1)",
        "/a\nb",
        "/" + "a" * 2048,
    ],
)
def test_falls_back_to_root(value: str | None) -> None:
    assert safe_return_to(value) == "/"


def test_with_query_keeps_existing_query_and_fragment() -> None:
    assert with_query("/p?tab=x#top", authError="access_denied") == "/p?tab=x&authError=access_denied#top"
