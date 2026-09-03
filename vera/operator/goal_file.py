"""The file a verification goal is talking about.

operator.run already accepts `path`, and resolves it to a session preview URL -
added after census 18, where "two runs spent 21 minutes clicking Vera's own
dashboard hunting for a timer that lived in timer.html". But it only helps a
caller that PASSES a path, and the planner frequently passes neither url nor
path, just a sentence.

Census 29, three of eight operator runs:

    goal "Observe the clock.html page loads, check if #clock div shows ..."
    -> landed on https://localhost:8999/  and clicked Vera's own UI (e30, e49)

With no url and no path the target falls back to the orchestrator root, which
is never the right place to verify a file the run just wrote. The goal usually
names the file; this reads it out of the sentence so the existing path
machinery can be used.

Scope, stated honestly: this only helps when the goal NAMES a file. Two of the
three census-29 failures said "the <div id='clock'> element" and "the timer"
without naming one, and this does nothing for those - that needs the run's
artifact registry, which the capability boundary does not have.

Pure: no I/O.
"""

from __future__ import annotations

import re
from typing import Optional

#: Extensions worth pointing a BROWSER at. A goal mentioning app.py is not
#: asking for it to be opened as a page.
WEB_SUFFIXES = (".html", ".htm", ".xhtml", ".svg")

#: A filename as it appears in prose: word characters, dots, dashes, and
#: underscores, ending in one of the suffixes above. Anchored on a boundary so
#: "myclock.html" is not matched from inside "notmyclock.html".
_FILE_RE = re.compile(
    r"(?<![\w/.-])([\w-]+(?:\.[\w-]+)*\.(?:html|htm|xhtml|svg))\b",
    re.IGNORECASE)


def filename_in_goal(goal: str) -> Optional[str]:
    """The web file a goal names, or None.

    Returns the FIRST match: a goal that names two files is ambiguous, and
    guessing which one matters is worse than leaving the target alone. The
    first is chosen rather than refusing outright because "open a.html and
    check it links to b.html" is common and the first is the subject.
    """
    text = str(goal or "")
    if not text.strip():
        return None
    # A goal that already carries a full URL needs no help from this - the
    # caller will use it, and pulling a filename out of a URL would turn an
    # absolute target into a relative guess.
    if re.search(r"https?://", text, re.IGNORECASE):
        return None
    m = _FILE_RE.search(text)
    if not m:
        return None
    name = m.group(1)
    # Defensive: the regex already guarantees this, but the suffix list is the
    # thing a reader will change, and it must stay the single source of truth.
    if not name.lower().endswith(WEB_SUFFIXES):
        return None
    return name
