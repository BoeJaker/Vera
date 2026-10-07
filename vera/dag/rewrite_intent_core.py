"""Does a step ASK for a file to be rewritten from scratch? - the pure test.

The write router's proven-file rule turns a code.author on a file that has
already run successfully into a code.edit, to protect working code. "Ran
successfully" is rc=0, not "was right": census 2026-09-30 analyse-data - the
script ran and printed mean 134 / std 0.00 for 200 random numbers (it read
one row); six code.edit calls then corrupted it, and when the controller's
recovery step said "Re-author complete analysis script from scratch" the rule
still redirected the re-author to code.edit. The rule blocked a rewrite in 25
of 277 census sessions (13 of the 58 that failed a check or hit the wall cap).

When the step itself asks for a rewrite, it gets one. Pure: text in, bool out.
"""

from __future__ import annotations

import re

_ASK = re.compile(
    r"\b(?:from\s+scratch|re-?author(?:ed|ing|s)?|re-?writ(?:e|ing|ten)\b(?!\s+(?:only|just)\b)"
    r"|start\s+(?:over|afresh)|fresh\s+(?:copy|version|script|file|implementation)"
    r"|replace\s+the\s+(?:whole|entire)\s+(?:file|script|module)"
    r"|regenerate\s+the\s+(?:whole|entire|complete)?\s*(?:file|script|module))",
    re.I)


def rewrite_asked(text: str) -> bool:
    """True when the step's title/goal explicitly asks for a from-scratch rewrite."""
    return bool(_ASK.search(str(text or "")))
