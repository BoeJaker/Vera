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

#: Arguments that hold a SHELL COMMAND rather than a plain target. They belong
#: in TARGET_ARGS - a command does say what is acted on - but for a shell the
#: command is ALSO the wording, and the executor rewords it exactly the way it
#: rewords `goal`:
#:
#:     python analyze_nums.py
#:     cd /workspace && python analyze_nums.py
#:     python3 ./analyze_nums.py
#:
#: Census run62, analyse-data: four attempts, one byte-identical traceback, four
#: different tally keys, nothing blocked - the very failure this module exists
#: to stop, on the most-repeated tool in every census (run62: exec.bash.run 9
#: repeats, 7 failures). So a command is reduced to its invariant before it is
#: keyed. See command_signature.
COMMAND_ARGS = ("command", "cmd")

#: Wrapper words that carry no identity - the executor swaps them between
#: attempts without changing what runs.
_CMD_PREFIX_NOISE = ("sudo", "time", "nohup", "exec", "command", "env")

#: Interpreter spellings that name the same interpreter.
_INTERPRETERS = {"python3": "python", "python2": "python", "py": "python",
                 "pytest3": "pytest"}

#: `cd somewhere && the-real-command` - only the tail decides the answer.
_CD_PREFIX = re.compile(r"^cd\s+[^&;|]+(?:&&|;)\s*(.+)$", re.I)

#: How many identical-kind failures against one target before the next is
#: refused. Two, so the third attempt is the one blocked: one failure can be
#: bad luck, two is the answer.
DEFAULT_LIMIT = 2

#: Leading reason codes the operator and the loop use, e.g.
#: "repeating_action: click was attempted 5 times...".
#:
#: Case-insensitive since 2026-09-22: the loop records the failure as
#: `"ERROR: " + str(error)` before handing it here, and an upper-case first
#: character made this pattern miss EVERY time. The kind then silently
#: degraded to a 120-char prefix, so two operator failures differing anywhere
#: in those characters (a url, an element ref, a title) read as different
#: kinds and never grouped - which is the whole job of this module.
_REASON_CODE = re.compile(r"^\s*([a-z][a-z0-9_]{2,40})\s*[:\-]", re.I)

#: The envelope the loop wraps an error in. Stripped before the reason code is
#: read, so "ERROR: repeating_action: ..." still keys on `repeating_action`.
_ENVELOPE = re.compile(r"^\s*(?:error|err|failed|failure|exception)\s*[:\-]\s*", re.I)

#: A python traceback's first 120 characters are boilerplate plus a file and a
#: line number - identical for two COMPLETELY different exceptions raised in the
#: same script. The prefix fallback below therefore grouped them as one kind, so
#: a run that fixed one bug and hit another was refused the next attempt as a
#: "repeat". run62's analyse-data raised an OverflowError and then a statistics
#: error from the same file; both keyed the same. A traceback's identity is its
#: LAST line - the exception type and message.
_TB_HEAD = "traceback (most recent call last)"


def _traceback_kind(raw: Any) -> str:
    """The exception line of a traceback, or "" when this is not one.

    Frames and source lines are indented and the exception line is not, so
    scanning from the end finds it without parsing. A traceback truncated
    mid-frame has no such line and the caller falls back to the prefix.
    """
    text = str(raw or "")
    if _TB_HEAD not in text.lower():
        return ""
    for line in reversed(text.splitlines()):
        if not line.strip() or line[:1].isspace():
            continue
        if _TB_HEAD in line.lower():
            continue
        return re.sub(r"\d+", "#", line.strip())[:120].lower()
    return ""


def command_signature(cmd) -> str:
    """A shell command reduced to what actually determines its answer.

    Drops a `cd <dir> &&` prefix, wrapper words (sudo/time/nohup/env), a
    leading `./` on any token, and normalises interpreter spellings
    (python3 -> python), so the rewordings above collapse to one key.
    Everything else - the program, its flags, its operands - is kept, because
    a genuinely different command is genuinely new information.
    """
    text = " ".join(str(cmd or "").split())
    if not text:
        return ""
    while True:
        m = _CD_PREFIX.match(text)
        if not m:
            break
        text = m.group(1).strip()
    parts = text.split()
    while parts and parts[0].lower() in _CMD_PREFIX_NOISE:
        parts.pop(0)
    if not parts:
        return ""
    base = parts[0].rsplit("/", 1)[-1]
    # Only a KNOWN interpreter spelling is rewritten. Anything else keeps its own
    # text: `PYTHONPATH=. python x.py` leads with an env assignment, and
    # case-folding that would rewrite the command rather than normalise it.
    parts[0] = _INTERPRETERS.get(base.lower(), base)
    return " ".join(t[2:] if t.startswith("./") else t for t in parts)


def target_key(tool: str, args: Optional[Dict[str, Any]]) -> str:
    """Identity of WHAT is being acted on - tool plus its target arguments.

    Returns just the tool when no target argument is present; that still groups
    attempts, and the failure KIND is the other half of the key.
    """
    parts = [str(tool or "").strip()]
    a = args if isinstance(args, dict) else {}
    for k in TARGET_ARGS:
        v = a.get(k)
        if v in (None, "", [], {}):
            continue
        if k in COMMAND_ARGS:
            v = command_signature(v)
            if not v:
                continue
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
    text = _ENVELOPE.sub("", text, count=1)
    if not text:
        return ""
    m = _REASON_CODE.match(text)
    if m:
        return m.group(1).lower()
    tb = _traceback_kind(error)
    if tb:
        return tb
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
