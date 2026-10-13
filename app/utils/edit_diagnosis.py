"""Finds where a model's search text went wrong in a file, for the retry after a rejected edit: the part of
it that is in the file, where the two part ways, and the file's real text there, so the model can copy it
instead of guessing again."""

from dataclasses import dataclass

# Characters of file text shown on each side of a place.
CONTEXT = 80
# Longest excerpt, so one minified line doesn't flood the prompt.
MAX_EXCERPT = 400
# A shared stretch shorter than this is chance, not the text the model meant.
MIN_MATCH = 12
# Longest anchor suggested; past this the text isn't a useful anchor.
MAX_ANCHOR = 200


@dataclass(frozen=True)
class NearMatch:
    """Where the model's search text and the file part ways.

    `offset` and `length` are the shared stretch in the file. `from_start` says which end of the search it
    is: True, the search starts like the file and then differs; False, it ends like the file. `wrote` and
    `file_has` are the search and the file around the point where they differ. `anchor` is the shortest text
    from the shared stretch onwards that appears once in the file, if there is one.
    """

    offset: int
    length: int
    from_start: bool
    wrote: str
    file_has: str
    anchor: str | None


def near_match(text: str, search: str) -> NearMatch | None:
    """The longest start or end of `search` that appears in `text`, or None if neither is long enough."""
    head = _longest_found(text, search, from_start=True)
    tail = _longest_found(text, search, from_start=False)
    offset, length, from_start = max(head, tail, key=lambda found: found[1])
    if length < MIN_MATCH:
        return None
    if from_start:
        # Search and file agree up to search[length]; show a little of what they agreed on, then the rest.
        back = min(length, 30)
        wrote = search[length - back : length + 50]
        file_has = text[offset + length - back : offset + length + 50]
    else:
        # The search's last `length` characters are the file from `offset`; they differ just before it.
        split = len(search) - length
        back = min(split, 50)
        wrote = search[split - back : split + 30]
        file_has = text[max(0, offset - back) : offset + 30]
    return NearMatch(offset, length, from_start, wrote, file_has, unique_anchor(text, offset))


def unique_anchor(text: str, offset: int) -> str | None:
    """The shortest text starting at `offset` (at least MIN_MATCH characters) that appears once in `text`."""
    longest = min(MAX_ANCHOR, len(text) - offset)
    if longest < MIN_MATCH or not _is_unique(text, text[offset : offset + longest]):
        return None
    lo, hi = MIN_MATCH, longest
    while lo < hi:  # Longer text from the same start stays unique, so the first unique length can be bisected.
        mid = (lo + hi) // 2
        if _is_unique(text, text[offset : offset + mid]):
            hi = mid
        else:
            lo = mid + 1
    return text[offset : offset + lo]


def occurrences(text: str, needle: str, limit: int = 5) -> list[int]:
    """Offsets of the first `limit` places `needle` appears in `text`."""
    found: list[int] = []
    at = text.find(needle) if needle else -1
    while at != -1 and len(found) < limit:
        found.append(at)
        at = text.find(needle, at + 1)
    return found


def excerpt(text: str, start: int, end: int | None = None) -> str:
    """The text from `start` to `end` with CONTEXT characters around it, marked with … where it's cut."""
    end = start if end is None else max(start, end)
    a = max(0, start - CONTEXT)
    b = min(len(text), end + CONTEXT, a + MAX_EXCERPT)
    return ("…" if a > 0 else "") + text[a:b] + ("…" if b < len(text) else "")


def line_col(text: str, offset: int) -> str:
    """`line N, column M` (both 1-based) of an offset."""
    line = text.count("\n", 0, offset) + 1
    column = offset - (text.rfind("\n", 0, offset) + 1) + 1
    return f"line {line}, column {column}"


def _longest_found(text: str, search: str, *, from_start: bool) -> tuple[int, int, bool]:
    """(offset in text, length, from_start) of the longest start (or end) of `search` found in `text`."""

    def piece(n: int) -> str:
        return search[:n] if from_start else search[len(search) - n :]

    lo, hi = 0, len(search)
    while lo < hi:  # A shorter start (or end) of a found piece is found too, so the length can be bisected.
        mid = (lo + hi + 1) // 2
        if text.find(piece(mid)) != -1:
            lo = mid
        else:
            hi = mid - 1
    return (text.find(piece(lo)) if lo else -1), lo, from_start


def _is_unique(text: str, needle: str) -> bool:
    first = text.find(needle)
    return first != -1 and text.find(needle, first + 1) == -1
