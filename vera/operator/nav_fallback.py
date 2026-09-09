"""The page a verification goal means, when the sentence never names it.

`operator.run` resolves its target in three steps: an explicit `url`, an
explicit `path`, or -- since census 29 -- a filename read out of the goal text
by `goal_file.filename_in_goal`. When all three come up empty the target falls
back to the orchestrator root, and the run spends its whole budget operating
Vera's own dashboard. That has now been recorded four times:

    census 18   two runs, 21 minutes, "hunting for a timer that lived in
                timer.html, because no URL was passed"
    census 29   three of eight runs landed on https://localhost:8999/
    census 30   a reused session stayed on the dashboard
    2026-09-09  author-then-edit re-test, third operator.run: goal
                "Verify 90-second countdown behavior" -- no url, no path, no
                filename in the sentence. 504s observing
                'VERA localhost:8999/ws/mcp ... OLLAMA 0 active' before
                stopping on time_budget. The two operator.run calls BEFORE it
                in the same run had both been given the correct preview URL.

goal_file's docstring names exactly this residue and why it stopped there:

    "this only helps when the goal NAMES a file ... that needs the run's
     artifact registry, which the capability boundary does not have."

It does have it now. `exec_capabilities.artifact_list_files(session_id=...)` is
already reached across that boundary by the missing-path hint, so the same
listing can answer "which page did this run write". This module is the pure
half of that: given the workspace listing, say which page the goal must have
meant, or say nothing.

WHY IT REFUSES TO GUESS. Returning the wrong page is not obviously better than
the dashboard -- it looks like it worked. So one page is an answer and two are
not: with several the caller keeps its existing behaviour, and the operator's
own missing-target reporting stays responsible for saying so. The nav pin
(nav_pin.py) then binds normally, because a resolved preview URL IS pinnable --
in the re-test the pin never failed, it was simply never armed.

Pure: no I/O, no session, no browser.
"""

from __future__ import annotations

import os
from typing import Any, Iterable, List, Optional

#: Extensions a browser renders as a page in its own right. Deliberately not
#: ".js"/".css"/".json": those are things a page LOADS, and pointing a browser
#: at one shows source, not a running interface -- which is what a verification
#: goal is always asking about.
PAGE_EXT = (".html", ".htm")


def is_page(name: str) -> bool:
    """Is this a file a browser can be pointed at to see a running UI?"""
    return os.path.splitext(str(name or "").strip().lower())[1] in PAGE_EXT


def page_candidates(names: Optional[Iterable[Any]]) -> List[str]:
    """The pages in a workspace listing, in order, without duplicates."""
    out: List[str] = []
    for n in (names or []):
        s = str(n or "").strip()
        if s and is_page(s) and s not in out:
            out.append(s)
    return out


def sole_page(names: Optional[Iterable[Any]]) -> str:
    """The one page this run wrote, or "" when that is not a single answer.

    Empty for no pages AND for several: see the module note -- a wrong page is
    worse than no page, because it reports as a real observation.
    """
    pages = page_candidates(names)
    return pages[0] if len(pages) == 1 else ""


def pin_for(url: str, inferred: bool, is_pinnable) -> str:
    """The URL to hold the run to, or "" for a run that stays free.

    An inferred target is never pinned. The pin holds a run to a target it was
    TOLD to use; a page picked out of the workspace was never told, and pinning
    it would trap a goal that really did mean the open web on a local file it
    never asked for -- worse than the dashboard this fallback exists to avoid.
    Starting in the right place is the benefit; enforcing it is not ours to
    claim.

    `is_pinnable` is passed in rather than imported so this stays pure and the
    scope rule keeps living in nav_pin, where it is already tested.
    """
    if inferred:
        return ""
    return str(url or "") if is_pinnable(url) else ""


def target_note(name: str) -> str:
    """Say where the target came from, so a reader is not left guessing.

    The caller had no url, no path and no filename in its sentence, so the
    resolved page is an INFERENCE and must be legible as one.
    """
    if not name:
        return ""
    return ("no url or path was given and the goal names no file, so the "
            "target was taken from the one page this run wrote (%s)" % name)
