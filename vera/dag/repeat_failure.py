"""Stop re-buying a failure you have already been told about.

Census 39, author-then-edit. Steps 1 and 2 took ONE cycle each and produced a
correct artifact - quality 4/4, including the display check. The entire 1807s
wall went on step 3:

    operator.run  ->  repeating_action: click attempted 5 times ... same url
    operator.run  ->  repeating_action: click attempted 5 times ... same url
    operator.run  ->  repeating_action: click attempted 5 times ... same url

Three calls, the same target, the same reason, no new information. The goal was
finished on disk after two cycles and the run still hit its cap.

WHY THE EXISTING GUARD DID NOT FIRE. `_v5_call_sig` keys on tool + ARGS, so a
verbatim repeat is caught. The executor rewords `goal` on each attempt - "Click
Start and observe...", "Load timer.html and verify..." - so the signature is
different every time even though the call is the same request against the same
page. Prose changed; the question did not.

So this keys on what actually determines the answer: the tool, the TARGET it
acts on, and the KIND of failure it came back with. Two identical-kind failures
against the same target is enough; the third is refused and the reason handed
back instead.

Deliberately NOT a general retry ban. A different target, or the same target
failing a DIFFERENT way, is new information and runs normally - the point is to
stop paying repeatedly for an answer already given.

Pure: no I/O, no clock.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional, Tuple

#: Arguments that identify WHAT a call acts on. Free-text arguments (goal, task,
#: query, prompt) are deliberately excluded: they are how the executor rewords
#: the same request, and including them is exactly why the existing signature
#: never matched across attempts.
TARGET_ARGS = ("url", "start_url", "path", "file", "filename", "filepath",
               "host", "command", "cmd", "session_id", "branch", "id")

#: How many identical-kind failures against one target before the next is
#: refused. Two, so the third attempt is the one blocked: one failure can be
#: bad luck, two is the answer.
DEFAULT_LIMIT = 2

#: Leading reason codes the operator and the loop use, e.g.
#: "repeating_action: click was attempted 5 times...".
_REASON_CODE = re.compile(r"^\s*([a-z][a-z0-9_]{2,40})\s*[:\-]")


def target_key(tool: str, args: Optional[Dict[str, Any]]) -> str:
    """Identity of WHAT is being acted on - tool plus its target arguments.

    Returns just the tool when no target argument is present; that still groups
    attempts, and the failure KIND is the other half of the key.
    """
    parts = [str(tool or "").strip()]
    a = args if isinstance(args, dict) else {}
    for k in TARGET_ARGS:
        v = a.get(k)
        if v not in (None, "", [], {}):
            parts.append("%s=%s" % (k, str(v)[:200]))
    return "|".join(parts)


def failure_kind(error: Any) -> str:
    """The KIND of failure, not its wording.

    Prefers a leading reason code ("repeating_action:", "time_budget:") because
    that is exactly what the operator returns and what makes two attempts the
    same answer. Falls back to a normalised prefix of the message so
    message-style errors still group.
    """
    text = " ".join(str(error or "").split())
    if not text:
        return ""
    m = _REASON_CODE.match(text)
    if m:
        return m.group(1).lower()
    # Digits differ between otherwise identical failures (counts, sizes, ports),
    # so they are not part of the kind.
    return re.sub(r"\d+", "#", text[:120]).strip().lower()


def record(seen: Optional[Dict[Tuple[str, str], int]], tool: str,
           args: Optional[Dict[str, Any]], error: Any) -> Dict[Tuple[str, str], int]:
    """Count one failure. Returns the (possibly new) tally."""
    tally = dict(seen or {})
    kind = failure_kind(error)
    if not kind:
        return tally
    key = (target_key(tool, args), kind)
    tally[key] = tally.get(key, 0) + 1
    return tally


def count(seen: Optional[Dict[Tuple[str, str], int]], tool: str,
          args: Optional[Dict[str, Any]], error: Any) -> int:
    """How many times this target has already failed this way."""
    kind = failure_kind(error)
    if not kind:
        return 0
    return int((seen or {}).get((target_key(tool, args), kind), 0))


def blocked_kind(seen: Optional[Dict[Tuple[str, str], int]], tool: str,
                 args: Optional[Dict[str, Any]],
                 limit: int = DEFAULT_LIMIT) -> str:
    """The failure kind this call would repeat, or "".

    Checked BEFORE the call, where the error is not yet known - so it asks
    whether this target has already failed some way `limit` times.
    """
    lim = max(1, int(limit or DEFAULT_LIMIT))
    key_prefix = target_key(tool, args)
    for (target, kind), n in (seen or {}).items():
        if target == key_prefix and n >= lim:
            return kind
    return ""


def describe(tool: str, kind: str, error: Any, limit: int = DEFAULT_LIMIT) -> str:
    """What to tell the executor instead of running the call again."""
    return (
        "`%s` has already failed this way %d times against the same target "
        "(%s). Running it again returns the same answer, not a new one:\n%s\n"
        "That is settled - do NOT call it a third time. Either act on what it "
        "told you (if it reported what it saw, treat that as the finding and "
        "fix the underlying file), choose a genuinely different approach, or "
        "emit `done` with what you already have."
        % (str(tool), max(1, int(limit or DEFAULT_LIMIT)), kind,
           str(error or "")[:600])
    )
