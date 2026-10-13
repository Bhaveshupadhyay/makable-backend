from app.utils.edit_diagnosis import (
    MAX_EXCERPT,
    count_occurrences,
    excerpt,
    interior_match,
    line_col,
    line_matches,
    near_match,
    occurrences,
    unique_anchor,
)

# A minified bundle, as in the Hublend template: the list is one item of a comma chain, not its own `const`.
BUNDLE = (
    'const a=1,b=()=>e.jsx("div",{})})),q=[[{name:"JavaScript",icon:"js.svg"},{name:"HTML",icon:"html.svg"}]],'
    'I=()=>e.jsx("h2",{children:"Tech Stack"});export{I as T};'
)


def test_a_search_whose_end_is_in_the_file_shows_where_the_start_differs() -> None:
    search = 'const q=[[{name:"JavaScript",icon:"js.svg"},{name:"HTML",icon:"html.svg"}]],'

    near = near_match(BUNDLE, search)

    assert near is not None
    assert not near.from_start
    assert near.offset == BUNDLE.index('q=[[{name:"JavaScript"')
    assert near.wrote == 'const q=[[{name:"JavaScript",icon:"j'
    assert near.file_has == '})})),q=[[{name:"JavaScript",icon:"j'
    assert near.anchor == 'q=[[{name:"J'


def test_a_search_whose_start_is_in_the_file_shows_where_the_rest_differs() -> None:
    search = 'e.jsx("h2",{children:"Tech Stack!"})'

    near = near_match(BUNDLE, search)

    assert near is not None
    assert near.from_start
    assert near.length == len('e.jsx("h2",{children:"Tech Stack')
    assert near.wrote.endswith('Stack!"})')
    assert near.file_has.endswith('Stack"});export{I as T};')


def test_unrelated_text_has_no_near_match() -> None:
    assert near_match(BUNDLE, "function Footer() { return null }") is None


def test_the_anchor_is_the_shortest_unique_text_from_a_place() -> None:
    text = "name: x\nname: y\nname: xyz"

    assert unique_anchor(text, text.index("name: xyz")) is None  # Too short to be unique before the end.
    assert unique_anchor(BUNDLE, 0) == "const a=1,b="
    assert unique_anchor(BUNDLE, len(BUNDLE) - 5) is None


def test_places_lines_and_excerpts() -> None:
    text = "a\nbb\nccc bb"

    assert occurrences(text, "bb") == [2, 9]
    assert occurrences(text, "bb", limit=1) == [2]
    assert line_col(text, 9) == "line 3, column 5"
    assert excerpt("x" * 1000, 500, 510) == "…" + "x" * 170 + "…"
    assert len(excerpt("y" * 5000, 0, 5000)) == MAX_EXCERPT + 1
    assert excerpt("short", 1) == "short"


def test_a_piece_from_the_middle_is_found_and_grown_both_ways() -> None:
    search = "XXXXXXXXXXXXXXXXXXXX" + BUNDLE[40:100] + "YYYYYYYYYYYYYYYYYYYY"

    middle = interior_match(BUNDLE, search)

    assert middle is not None
    assert (middle.start, middle.offset, middle.length) == (20, 40, 60)
    assert interior_match(BUNDLE, "nothing like the bundle at all, really") is None


def test_counts_and_line_matches() -> None:
    assert count_occurrences("aaaa", "aa") == 3  # Overlapping, like the edit check's search.
    assert count_occurrences("abc", "") == 0
    text = "  if (a) {\n    go()\n  }\n  if (a) {\n    go()\n"
    assert line_matches(text, "\n\tif (a) {\n\t\tgo()\n  \n") == [0, text.index("  if (a) {", 5)]
    assert line_matches(text, "  \n") == []
