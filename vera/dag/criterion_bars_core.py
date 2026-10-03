"""A size/count bar the planner invented is a guide for the verifier, not a requirement.

Probe 3 Oct (850d0600, strategic-vector-db-report): the step goal was "Fetch and extract
detailed comparison data for 4 vector databases"; the planner's criterion added "two
web.fetch calls each returning >1500 words of structured text". The verifier, told to judge
strictly, failed the step three times on that number alone and the run spent its whole
30 minutes researching without writing report.md (the previous run wrote it in 15).
Since 26 Sep: 8 of 287 verifier failures hinged on such a number, 6 of them in that run.
Pure.
"""

from __future__ import annotations

import re
from typing import List

_BAR = re.compile(r"(?:>=?|≥|at least|minimum of|more than|over|no fewer than)\s*(\d[\d,]*)\s*"
                  r"(words|chars|characters|bytes|lines|calls|results|sources|urls|items|rows|"
                  r"records|pages|sections|paragraphs)?", re.I)


def invented_bars(criterion: str, step_goal: str) -> List[str]:
    """The numeric bars in `criterion` whose number the step goal never states."""
    have = set(n.replace(",", "") for n in re.findall(r"\d[\d,]*", step_goal or ""))
    out: List[str] = []
    for m in _BAR.finditer(criterion or ""):
        n = m.group(1).replace(",", "")
        if n not in have:
            txt = " ".join(m.group(0).split())
            if txt not in out:
                out.append(txt)
    return out


def note(criterion: str, step_goal: str) -> str:
    bars = invented_bars(criterion, step_goal)
    if not bars:
        return ""
    return ("NOTE ON THE CRITERION: its numeric bar(s) %s were set by the planner and appear "
            "nowhere in the STEP GOAL. Treat them as a rough guide: judge whether the step "
            "produced the substance the criterion describes, and do not fail it ONLY for "
            "falling short of such a number.\n" % ", ".join(repr(b) for b in bars))
