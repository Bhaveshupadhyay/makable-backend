"""Reads a file out of the model's reply: the contents of its (first, outermost) fenced code block."""

import re

_FENCE = re.compile(r"^(`{3,}|~{3,})[^\n]*\n", re.MULTILINE)


def extract_code(reply: str) -> str:
    """The file in a reply: what's between its first opening fence and that fence's closing line.

    Raises:
        ValueError: The reply has no complete fenced code block.
    """
    opening = _FENCE.search(reply)
    if opening is None:
        raise ValueError("the reply has no code block: put the whole file in one ``` code block")
    fence = opening.group(1)
    closing = re.compile(rf"^{re.escape(fence[0])}{{{len(fence)},}}[ \t]*$", re.MULTILINE)
    end = closing.search(reply, opening.end())
    if end is None:
        raise ValueError("the code block isn't closed: the file may have been cut off; write it again, shorter")
    code = reply[opening.end() : end.start()]
    return code if code.endswith("\n") else code + "\n"
