"""What a copied line-number gutter looks like, in one place.

An edit anchors on text that must match the file EXACTLY, and the editor is
shown the file with line numbers so it can orient itself. It then copies the
anchor WITH the numbers attached, and the edit can never match. Both the block
parser and the loop's late fallback strip that gutter, and until 2026-09-22
both carried their own copy of the same pattern:

    _GUTTER_RE = re.compile(r"^\\s*\\d+\\s*\\|\\s?")

which matches the shape Vera actually emits (`_v5_numbered`: ``"%5d | %s"``)
and nothing else. Census run62, prose-only: the editor re-rendered that view as
a MARKDOWN TABLE and copied the anchor from its own rendering -

    ### 2. Caching Example
    | 18 |
    | 19 | A cache stores frequently accessed data

- so every line carried a LEADING pipe as well. Neither copy recognised it, the
anchor kept its gutter, `find` could not match, and the goal spent 1,236s
failing to edit a file that was already correct.

The leading pipe is not something Vera ever emits; it is the editor's own
embellishment. It is still a gutter, so it is recognised as one here.

Pure: strings in, strings out.
"""
from __future__ import annotations

import re
from typing import List, Sequence

#: A line-number gutter. `  98 | code` is what `_v5_numbered` emits; the
#: leading `|` is the markdown-table rendering the editor sometimes copies
#: from (run62). The trailing single space is optional so `| 18 |` - a
#: guttered BLANK line, which is how a table renders one - is matched too.
GUTTER_RE = re.compile(r"^[ \t]*\|?[ \t]*\d+[ \t]*\|[ \t]?")


def looks_guttered(line: str) -> bool:
    """True when this single line starts with a line-number gutter."""
    return bool(GUTTER_RE.match(line or ""))


def strip_line(line: str) -> str:
    """The line without its gutter (unchanged when it has none)."""
    return GUTTER_RE.sub("", line or "", count=1)


def strip_text(text: str) -> str:
    """Strip a gutter from EVERY line that has one.

    Unconditional, for the caller that uses it as a late fallback: it tries the
    anchor verbatim first and only reaches for this when the anchor is absent
    AND the stripped form is present in the file, so a false positive cannot
    reach the file.
    """
    if not text:
        return text or ""
    return "\n".join(strip_line(l) for l in text.split("\n"))


def strip_uniform(lines: Sequence[str]) -> List[str]:
    """Strip only when EVERY non-blank line is guttered.

    For the parse-time caller, which has no file to check against: one
    `12 | x` among ordinary lines is far more likely to be real content - a
    markdown table row, a shell pipe - than a gutter, and leaving it alone
    fails safely (the anchor simply will not match).
    """
    rows = list(lines or [])
    real = [l for l in rows if l.strip()]
    if not real or not all(looks_guttered(l) for l in real):
        return rows
    return [strip_line(l) if l.strip() else l for l in rows]
