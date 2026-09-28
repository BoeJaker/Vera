"""When an edit anchor is not in the file, say what IS.

`_v5_apply_edits` refuses an anchor that matches zero times or more than once.
The two refusals were not equally useful:

  * MORE than once - names the exact line numbers it matched and tells the
    editor to extend the anchor with an adjacent unique line. Added after a
    census finding on 2026-08-29.
  * ZERO times - echoed the model's OWN `find` text back at it:
    "`find` text not present in the file (first 60 chars: 'let rema...')".
    That is the one thing the model already knows. Nothing about the file.

Census run 16 measured the cost. In `author-then-edit` step 3, `code.edit`
failed that way at cycle 10 (32.6s) and then again, identically, at cycle 14
(32.0s) - a minute of GPU time spent twice on the same mistake, because the
first failure gave the retry nothing to correct with.

Almost always the anchor is nearly right: copied with the line-number gutter
attached (already stripped and retried upstream), whitespace collapsed, or
reconstructed from memory a word out. So show the closest lines that really
exist, with their numbers, and name the whitespace case explicitly because it
is the least visible on screen.

One thing worth knowing, because it narrows what this module is FOR: the anchor
is matched as a SUBSTRING of the whole file, not line by line. A single-line
anchor that merely lost its leading indentation therefore still matches, and
never reaches here (verified against the real `_v5_apply_edits`). The
whitespace hint earns its place on MULTI-LINE anchors, where the newline and
the indentation of every line after the first are part of the text being
matched - which is exactly the shape the model reconstructs from memory.

Pure: text in, text out. No I/O, no app imports.
"""

from __future__ import annotations

import difflib
from typing import Any, Dict, List

#: Below this similarity a "closest line" is noise and would mislead more than
#: help, so nothing is offered rather than something wrong.
FLOOR = 0.55

#: Never quote more than this back into a prompt.
MAX_LINES = 3
CLIP = 120


def _norm(s: str) -> str:
    return " ".join((s or "").split())


def _ratio(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a, b).ratio()


def whitespace_only_match(content: str, find: str) -> List[int]:
    """1-based lines equal to `find` once whitespace is normalised.

    The highest-value hint there is: the anchor IS in the file, just indented
    differently or with runs of spaces collapsed - invisible when reading, and
    the model cannot see why an anchor it copied does not match.
    """
    want = _norm(find)
    if not want:
        return []
    lines = (content or "").splitlines()
    want_n = len(find.splitlines()) or 1
    hits: List[int] = []
    if want_n == 1:
        for i, line in enumerate(lines, 1):
            if _norm(line) == want:
                hits.append(i)
    else:
        for i in range(len(lines) - want_n + 1):
            if _norm("\n".join(lines[i:i + want_n])) == want:
                hits.append(i + 1)
    return hits[:MAX_LINES]


def whitespace_only_span(content: str, find: str):
    """The file's OWN text for an anchor that differs only in whitespace.

    whitespace_only_match already tells the model "it DOES appear at line N
    apart from whitespace" - a hint it then usually failed to act on, because
    re-copying an anchor by hand is exactly what it just got wrong. Census 33:
    two of six code.edit failures were this, on files the run had itself
    written moments earlier.

    Returns the verbatim slice of `content` so the caller can anchor on the
    file's real text - indentation intact - rather than the model's version.
    None unless there is EXACTLY ONE such region: the one-match invariant is
    what stops a near-miss silently corrupting the wrong place, and a
    whitespace-insensitive compare makes collisions MORE likely, not less.
    """
    hits = whitespace_only_match(content, find)
    if len(hits) != 1:
        return None
    lines = (content or "").splitlines(keepends=True)
    start = hits[0] - 1
    height = len(find.splitlines()) or 1
    if start < 0 or start + height > len(lines):
        return None
    span = "".join(lines[start:start + height])
    # splitlines(keepends) leaves the trailing newline on the last line; the
    # anchor the caller replaces must not swallow it unless `find` had one.
    if span.endswith("\n") and not find.endswith("\n"):
        span = span[:-1]
    return span or None


def nearest_lines(content: str, find: str, *, limit: int = MAX_LINES,
                  floor: float = FLOOR) -> List[Dict[str, Any]]:
    """The most similar real lines, best first, as {line, text, ratio}.

    A multi-line anchor is compared against windows of the same height, so the
    suggestion is something the editor could actually anchor on.
    """
    lines = (content or "").splitlines()
    want = _norm(find)
    if not want or not lines:
        return []
    height = len(find.splitlines()) or 1
    scored: List[Dict[str, Any]] = []
    if height == 1:
        for i, line in enumerate(lines, 1):
            if not line.strip():
                continue
            r = _ratio(want, _norm(line))
            if r >= floor:
                scored.append({"line": i, "text": line.strip()[:CLIP], "ratio": round(r, 3)})
    else:
        for i in range(len(lines) - height + 1):
            window = lines[i:i + height]
            if not any(w.strip() for w in window):
                continue
            r = _ratio(want, _norm("\n".join(window)))
            if r >= floor:
                scored.append({"line": i + 1,
                               "text": " / ".join(w.strip() for w in window)[:CLIP],
                               "ratio": round(r, 3)})
    scored.sort(key=lambda d: (-d["ratio"], d["line"]))
    return scored[:limit]


#: A near miss this close, with nothing else nearly as close, is the line the
#: editor meant. Census run70-73 (2026-09-24): 11 anchors were refused with
#: "closest text actually in the file - line N" because the model quoted a
#: line from an EARLIER version it had itself edited; the hint named the line
#: and the retry re-typed it wrong again.
#: 0.80, not higher: an attribute appended inside a tag (`required>` -> `required
#: onblur=...>`) scores 0.82 against its own old line, and that is the census case.
REANCHOR_RATIO = 0.80
REANCHOR_GAP = 0.08


def nearest_unique_span(content: str, find: str, *, min_ratio: float = REANCHOR_RATIO,
                        gap: float = REANCHOR_GAP):
    """The file's OWN text for a stale anchor that names exactly one region.

    Containment first: the commonest stale anchor is a line the model's own
    earlier edit EXTENDED (an attribute appended, a call added), so the old
    text is still a whitespace-normalised substring of exactly one region.
    Otherwise similarity: >= `min_ratio`, with the next-best region at least
    `gap` behind (or none). None in every other case - two candidates that
    close means the model could have meant either, and guessing corrupts.
    """
    lines = (content or "").splitlines(keepends=True)
    height = len(find.splitlines()) or 1
    want = _norm(find)
    if not want or not lines or height > len(lines):
        return None
    # The closer moves when something is appended inside a tag or call:
    # `required>` became `required onblur=...>`, `f(a)` became `f(a, b)`.
    # A short anchor then fails both containment and similarity, so also
    # try it without its trailing closer.
    wants = [want]
    stripped = want.rstrip(">;),]}").rstrip()
    if stripped and stripped != want and len(stripped) >= 8:
        wants.append(stripped)
    contained = [i for i in range(len(lines) - height + 1)
                 if any(w in _norm("".join(lines[i:i + height])) for w in wants)]
    if len(contained) > 1:
        return None
    if len(contained) == 1:
        start = contained[0]
    else:
        near = nearest_lines(content, find, limit=2, floor=min_ratio)
        if not near:
            return None
        if len(near) > 1 and (near[0]["ratio"] - near[1]["ratio"]) < gap:
            return None
        start = near[0]["line"] - 1
    if start < 0 or start + height > len(lines):
        return None
    span = "".join(lines[start:start + height])
    if span.endswith("\n") and not find.endswith("\n"):
        span = span[:-1]
    return span or None


def describe_missing_anchor(content: str, find: str, *, edit_no: int = 1) -> str:
    """The whole error line for a zero-match anchor, hint included."""
    head = (f"edit {edit_no}: `find` text not present in the file "
            f"(first 60 chars: {find[:60]!r})")

    ws = whitespace_only_match(content, find)
    if ws:
        where = ", ".join(str(n) for n in ws)
        return (f"{head}. It DOES appear at line {where} apart from whitespace - "
                "your anchor differs only in indentation or spacing. Copy it "
                "from the file EXACTLY, all of it, including the leading spaces "
                "on EVERY line.")

    near = nearest_lines(content, find)
    if near:
        shown = "; ".join(f"line {n['line']}: {n['text']!r}" for n in near)
        return (f"{head}. Closest text actually in the file - {shown}. Anchor on "
                "one of those, copied EXACTLY as it appears (do not retype it "
                "from memory), or read the file again if none is the line you "
                "meant.")

    return (f"{head}. Nothing in the file resembles it, so the anchor is not a "
            "near miss - you are editing the wrong file, or the content you "
            "expect was never written. Read the file before editing it again.")
