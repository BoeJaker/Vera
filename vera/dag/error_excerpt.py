"""Show enough of a failure to diagnose it.

The loop trace clipped a failed call's error to 160 characters and the operator
trace to 200. For most errors that is fine - the message is the first sentence.
For the ones that matter most it is useless, because the tools that fail at
length put the DIAGNOSIS AT THE END:

    ============================= test session starts ==============================
    platform linux -- Python 3.12.14, pytest-9.1.1, pluggy-1.6.0 -- /usr/local/bin/…

That is the whole of what census 39's build-multifile recorded for every one of
its failing pytest runs. The actual cause -

    assert calculate_mode([-1, 2, -3, 4, -5, 2]) == [2]
    E   assert 2 == [2]
    ...
    7 failed, 13 passed

- was hundreds of characters further down, and had to be recovered by re-running
pytest by hand in the run's own sandbox. A trace that records the banner and
drops the verdict is not a record of the failure.

So keep BOTH ends: the head says what ran, the tail says what went wrong. The
middle of a long traceback or a pytest run is the part a reader skips anyway.
"""

from __future__ import annotations

from typing import Any

#: Characters kept from the start - enough for the command, the banner and the
#: first line of a message-style error.
HEAD = 400

#: Characters kept from the end - where pytest puts its summary, where a Python
#: traceback puts the exception, and where a compiler puts the error count.
TAIL = 1400

#: Marker for what was dropped. Explicit, so nobody reads a clipped trace as the
#: complete output.
ELLIPSIS = "\n… [%d characters omitted] …\n"


def excerpt(value: Any, head: int = HEAD, tail: int = TAIL) -> str:
    """The head and tail of ``value``, with the omission stated.

    Short text is returned unchanged, so nothing that already fitted changes.
    """
    text = "" if value is None else str(value)
    h = max(0, int(head))
    t = max(0, int(tail))
    if len(text) <= h + t:
        return text
    dropped = len(text) - h - t
    return text[:h] + (ELLIPSIS % dropped) + (text[len(text) - t:] if t else "")


def is_truncated(text: str) -> bool:
    """True when ``text`` is an excerpt rather than the whole thing."""
    return "characters omitted" in str(text or "")
