from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

MAX_LENGTH = 2048


def safe_return_to(value: str | None) -> str:
    """A same-origin path to send the browser back to after login, or `/`.

    `returnTo` comes from the query string, so it's untrusted. Mirrors the SPA's `safeReturnTo`:
    `//evil.com` and `/\\evil.com` (browsers treat `\\` like `/`) would otherwise be open redirects.

    Args:
        value: The requested path.

    Returns:
        `value` if it's a plain same-origin path, otherwise `/`.
    """
    if not value or len(value) > MAX_LENGTH or not value.startswith("/") or value.startswith("//"):
        return "/"
    if "\\" in value or any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        return "/"
    parts = urlsplit(value)
    if parts.scheme or parts.netloc:
        return "/"
    return value


def with_query(path: str, **params: str) -> str:
    """Adds query parameters to a path, keeping its existing query and fragment."""
    parts = urlsplit(path)
    query = urlencode([*parse_qsl(parts.query), *params.items()])
    return urlunsplit(("", "", parts.path, query, parts.fragment))
