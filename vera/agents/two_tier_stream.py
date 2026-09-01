"""Keep the continuation marker off the user's screen without delaying tokens.

Tier 1 ends with `[[NEEDS-CONTEXT]]` when it needs the second pass. The user must
never see it, and the marker does not arrive whole: a stream delivers it in
whatever pieces the tokeniser produced, so `"[[NEEDS"` and `"-CONTEXT]]"` can land
in separate chunks - and a chunk-by-chunk `replace()` would emit the first half
before the second arrived.

Buffering the entire reply until the end would remove the marker and the point of
the feature with it. So hold back only as much tail as could be the start of the
marker - at most len(marker)-1 characters - and release everything else
immediately. A token is delayed only when it genuinely looks like the beginning
of the marker, which is the one case where emitting early is wrong.

Pure: text in, text out, no I/O.
"""

from __future__ import annotations

from typing import List

MARKER = "[[NEEDS-CONTEXT]]"


def _longest_suffix_prefix(text: str, marker: str) -> int:
    """Length of the longest suffix of `text` that is a prefix of `marker`."""
    n = min(len(text), len(marker) - 1)
    for size in range(n, 0, -1):
        if text[-size:] == marker[:size]:
            return size
    return 0


class MarkerFilter:
    """Streaming filter that removes MARKER and reports whether it appeared.

    Usage: feed() every chunk and emit what it returns, then flush() at the end.
    `seen` is the decision tier 2 hangs off, so it must survive the marker being
    split across any number of chunks.
    """

    def __init__(self, marker: str = MARKER) -> None:
        self.marker = marker or MARKER
        self._buf = ""
        self.seen = False
        self._out: List[str] = []

    def feed(self, text: str) -> str:
        """Safe-to-emit text for this chunk (may be empty)."""
        self._buf += str(text or "")
        while self.marker in self._buf:
            self.seen = True
            self._buf = self._buf.replace(self.marker, "", 1)
        hold = _longest_suffix_prefix(self._buf, self.marker)
        emit = self._buf[:len(self._buf) - hold] if hold else self._buf
        self._buf = self._buf[len(emit):]
        self._out.append(emit)
        return emit

    def flush(self) -> str:
        """Whatever was held back, once no more can arrive."""
        rest, self._buf = self._buf, ""
        if self.marker in rest:
            self.seen = True
            rest = rest.replace(self.marker, "")
        self._out.append(rest)
        return rest

    @property
    def text(self) -> str:
        """Everything emitted so far, marker removed - what the user has seen."""
        return "".join(self._out)


NO_ADDITION = "[[NO-ADDITION]]"


class NoAdditionGate:
    """Withhold tier 2's output until "the context adds nothing" is ruled out.

    Under the tier2 decider the second pass decides by what it EMITS, so its
    first tokens cannot go straight to the screen: if they turn out to be
    NO_ADDITION, the user must see nothing at all. Once enough has arrived to
    show it is not that sentinel, everything held is released and the rest
    streams normally.

    The delay is bounded by the sentinel's length and applies only to tier 2 -
    tier 1 has already answered, so the user is reading while this resolves.
    """

    def __init__(self, sentinel: str = NO_ADDITION) -> None:
        self.sentinel = sentinel or NO_ADDITION
        self._buf = ""
        self._decided = False
        self.suppressed = False

    def feed(self, text: str) -> str:
        if self.suppressed:
            return ""
        if self._decided:
            return str(text or "")
        self._buf += str(text or "")
        probe = self._buf.lstrip()
        if not probe:
            return ""                                   # only whitespace so far
        if probe.startswith(self.sentinel):
            self.suppressed = self._decided = True
            self._buf = ""
            return ""
        if self.sentinel.startswith(probe):
            return ""                                   # still could become it
        self._decided = True
        out, self._buf = self._buf, ""
        return out

    def flush(self) -> str:
        """Release anything still held once the stream has ended."""
        if self.suppressed:
            return ""
        if self._buf.lstrip() == self.sentinel.rstrip():
            self.suppressed = True
            self._buf = ""
            return ""
        out, self._buf = self._buf, ""
        self._decided = True
        return out
