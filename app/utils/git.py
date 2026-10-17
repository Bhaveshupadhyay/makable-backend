"""Git's own hashing, so a SHA computed here matches the one GitHub gives the same content."""

import hashlib


def git_blob_sha(content: str | bytes) -> str:
    """The SHA git (and so GitHub) gives a file with this content."""
    data = content.encode() if isinstance(content, str) else content
    return hashlib.sha1(b"blob %d\0" % len(data) + data, usedforsecurity=False).hexdigest()
