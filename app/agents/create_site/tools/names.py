"""A name to suggest for a new site, from the user's prompt (no model call: it's only a suggestion)."""

import re

# Words that describe the request rather than name the site.
_FILLER = frozenset(
    [
        "a",
        "an",
        "the",
        "my",
        "our",
        "your",
        "for",
        "of",
        "and",
        "with",
        "in",
        "on",
        "to",
        "at",
        "by",
        "from",
        "make",
        "create",
        "build",
        "design",
        "generate",
        "need",
        "want",
        "i",
        "we",
        "please",
        "new",
        "simple",
        "modern",
        "beautiful",
        "nice",
        "website",
        "web",
        "site",
        "page",
        "landing",
        "homepage",
        "home",
        "one",
        "that",
        "this",
        "called",
        "named",
        "me",
        "us",
        "can",
        "you",
        "some",
    ]
)
MAX_SUGGESTION_WORDS = 4
# Well under what a question's option (`MAX_OPTION`) and a site's name (`SITE_NAME_MAX`) can be.
MAX_SUGGESTION_CHARS = 40


def suggest_name(prompt: str) -> str:
    """A quoted name in the prompt if there is one, else its first few meaningful words, in title case; at most
    `MAX_SUGGESTION_CHARS` long."""
    quoted = re.search(r"\"([^\"]{2,40})\"|“([^”]{2,40})”|(?<!\w)'([^']{2,40})'(?!\w)", prompt)
    if quoted:
        return next(g for g in quoted.groups() if g).strip()
    words = [w for w in re.findall(r"[A-Za-z0-9][A-Za-z0-9&'-]*", prompt) if w.lower() not in _FILLER]
    name = ""
    for word in words[:MAX_SUGGESTION_WORDS]:
        if len(name) + len(word) + 1 > MAX_SUGGESTION_CHARS:
            break
        name = f"{name} {word}".strip()
    if not name and words:
        name = words[0][:MAX_SUGGESTION_CHARS]
    return name.title() if name else "My site"
