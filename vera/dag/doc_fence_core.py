"""A document the writer wrapped in an UNCLOSED fence keeps no fence line.

Live 2026-10-03 (session 6ef06017): prose.author's reply opened with
```markdown file=why_tests.md and never closed it, so block extraction found nothing,
the plain-text fallback saved the reply as it was, and the delivered why_tests.md began
with that fence line. code.author has _v5_clean_code_body for the same failure; a
document needs a gentler version - a markdown file may legitimately END with its own
code block, so a trailing ``` is removed only when the fences left are unbalanced.
Pure.
"""

from __future__ import annotations

import re

#: The writer's own wrapper: a document language, or any fence carrying file=. A bare
#: ``` or ```python is left alone - it may be the document's own content.
_WRAPPER_OPEN = re.compile(r"^\s*```\s*(?:markdown|md|text|txt|rst|html)\s*(?:file=\S*)?\s*$"
                           r"|^\s*```[^\n`]*\bfile=[^\n`]*$",
                           re.IGNORECASE)
_FENCE = re.compile(r"^\s*```")


def unwrap(text: str) -> str:
    """Drop the writer's own wrapper fence(s) from a document body."""
    lines = (text or "").splitlines()
    i = 0
    while i < len(lines) and not lines[i].strip():
        i += 1
    if i >= len(lines) or not _WRAPPER_OPEN.match(lines[i]):
        return text
    body = lines[i + 1:]
    fences = [k for k, ln in enumerate(body) if _FENCE.match(ln)]
    if fences and len(fences) % 2 == 1 and body[fences[-1]].strip().strip("`") == "":
        # odd count: the last bare ``` closes the wrapper we just removed
        tail = fences[-1]
        if all(not ln.strip() for ln in body[tail + 1:]):
            body = body[:tail]
    out = "\n".join(body).strip("\n")
    return out + ("\n" if text.endswith("\n") else "")
