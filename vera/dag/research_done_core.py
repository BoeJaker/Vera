"""A research-only step with its sources in hand is answered (plan item 17e).

Census run74-76 (25 Sep 2026): after web.research had returned its sources
(text inline, URLs) the executor kept going - web.search, web.fetch, another
web.research - 14, 3 and 27 such calls per set, every one an executor turn of
~20 s, and the research goals were the slowest family left (research-report
1,727 s in run76 with 4 / 15 / 12 / 12 web.* calls across its research steps).
The planner's research directive already says ONE web.research per question.

The rule: in a step whose planned caps are research (and reading) caps only,
once web.research has succeeded twice the sources are in hand - a further
research call is served the latest research result with a note, and a second
one ends the step on it. A step that also plans authoring or running is left
alone: its research is a means, and the executor decides when it has enough.

Pure: step fields and counts in, a note or '' out.
"""
from __future__ import annotations

from typing import Iterable

RESEARCH_TOOLS = frozenset({"web.research", "web.search", "web.fetch", "http.get", "web.crawl"})
STEP_CAPS_OK = RESEARCH_TOOLS | frozenset({"sandbox.session.fs.read", "code.read", "ide.fs.read"})
#: Successful web.research results after which the step's sources are in hand.
ENOUGH_RESEARCH = 2
#: Served research calls after which the next one ends the step.
MAX_SERVED = 1


def saturated_note(step_caps: Iterable[str], tool: str, ok_research: int, latest: str) -> str:
    """The note that replaces a research call once the step's sources are in hand, else ''."""
    tool = str(tool or "")
    if tool not in RESEARCH_TOOLS:
        return ""
    caps = {str(c) for c in (step_caps or []) if c}
    if not caps or not caps <= STEP_CAPS_OK:
        return ""
    try:
        n = int(ok_research)
    except (TypeError, ValueError):
        return ""
    if n < ENOUGH_RESEARCH or not str(latest or "").strip():
        return ""
    return (f"this step's sources are IN HAND: web.research has already returned {n} result sets with "
            "their text inline, shown below. Another `%s` will not add what the step needs - the "
            "step is answered by what was found. State the finding (with its source URLs) and emit "
            "`done`; the write-up belongs to the step that authors the document.\n\n%s"
            % (tool, str(latest)[:2500]))


def should_end(served: int) -> bool:
    try:
        return int(served) > MAX_SERVED
    except (TypeError, ValueError):
        return False
