"""A command that succeeded and printed nothing has told you something.

Census run 16, build-multifile, step 8. Twelve calls, ten of which SUCCEEDED:

     5 exec.python.run .../tests/test_stats.py     rc=0  stdout=""
     6 python -m py_compile .../test_stats.py      rc=0  stdout=""
     7 (the same again)                            rc=0  stdout=""
     8 python3 -m py_compile ...                   rc=0  stdout=""
     9 python  -m py_compile ...                   rc=0  stdout=""
    10 ls -la ... && python3 -m py_compile ...     rc=0

It looks like thrash and it is not. `py_compile` on a valid file prints nothing;
running a pytest-style file directly prints nothing. So the model was handed
`{"ok": true, "rc": 0, "stdout": "", "stderr": ""}` five times and had no way to
tell whether it had verified anything — an empty string reads as "no
information", not as "the check passed". So it varied the command slightly and
tried again: `python` then `python3`, absolute then relative, `ls &&` prefixed.

The loop's guards were not at fault and were not bypassed. `success_sigs`
de-duplicates identical repeats and `failed_sigs`/`_MAX_REPEAT_FAIL` catch
identical failures — but these calls were neither identical nor failing. Nothing
was broken except the model's ability to conclude.

So the fix is not another guard. It is to stop shipping an ambiguous blank:
silence on success is a RESULT, and saying so costs one sentence.

## Where this is applied

At the loop's result-preview chokepoint (so every capability the model reads
gets it, not just the exec caps someone remembered to patch) AND on the exec
result dicts themselves (so the UI and any direct caller see it too). One
definition, because three hand-rolled copies of a PYTHONPATH prefix drifted in
exactly this file's neighbourhood and cost three days.

Pure: no app imports, no I/O.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

# Keys that mark a dict as an EXEC result rather than some other capability's
# payload. Both must be present: plenty of results carry an `ok`, and annotating
# a non-exec result would be noise at best and misleading at worst.
_RC_KEYS = ("rc", "exit_code", "returncode")
_OUT_KEYS = ("stdout", "stderr")

NOTE = ("the command SUCCEEDED (rc=0) and produced NO output — an empty stdout "
        "here is the result, not a missing one. Do not re-run it to check, and "
        "do not vary it slightly and retry: if you need visible output, ask for "
        "it explicitly (print, ls, echo); if the command's purpose was to "
        "succeed or to verify, it did.")

#: The mirror image, and the reason it needed its own note: `grep`, `test`,
#: `diff` and `pgrep` all exit 1 while printing NOTHING, and for them that is
#: how a NEGATIVE ANSWER is reported - not a fault. Census 41 and 43 produced
#: three of these, and the loop retried. The note deliberately does not tell the
#: model the call succeeded (ok stays False, the exit code is real); it tells it
#: what an empty rc=1 usually MEANS so it stops reading it as a broken call.
FAILURE_NOTE = (
    "the command exited {rc} and printed NOTHING to stdout or stderr. For "
    "grep, test, diff and pgrep that is exactly how they report NO MATCH / NOT "
    "TRUE - so this may be the ANSWER to what you asked, not a broken call. "
    "Re-running it unchanged will return the same thing. If you need the "
    "distinction, ask for it explicitly (`grep -c`, `... || echo NO-MATCH`, or "
    "check the exit code yourself); if you need the command to have produced "
    "output, the problem is upstream of this call.")

#: How much of the command to carry back. Enough to identify the call, not
#: enough to bloat every result.
MAX_COMMAND = 300


def _txt(v: Any) -> str:
    return v if isinstance(v, str) else ("" if v is None else str(v))


def looks_like_exec_result(res: Any) -> bool:
    """True for an exec-shaped result dict, false for everything else."""
    if not isinstance(res, dict):
        return False
    return (any(k in res for k in _RC_KEYS)
            and any(k in res for k in _OUT_KEYS))


def _rc_of(res: Dict[str, Any]) -> Optional[int]:
    for k in _RC_KEYS:
        if k in res:
            try:
                return int(res[k])
            except (TypeError, ValueError):
                return None
    return None


def is_silent_success(res: Any) -> bool:
    """A command that finished cleanly and wrote nothing to stdout OR stderr.

    Deliberately strict. A timed-out command is not a success even if it printed
    nothing, and a non-zero rc obviously is not — in both of those the blank
    output is genuinely a missing signal rather than an answer.
    """
    if not looks_like_exec_result(res):
        return False
    if res.get("timed_out"):
        return False
    rc = _rc_of(res)
    if rc is None or rc != 0:
        return False
    if res.get("ok") is False:
        return False
    return not (_txt(res.get("stdout")).strip() or _txt(res.get("stderr")).strip())


def is_silent_failure(res: Any) -> bool:
    """A command that exited NON-ZERO and wrote nothing to stdout OR stderr.

    Strict for the same reasons as its twin: a timeout is not a silent failure
    (it has its own cause, and `result_failure_reason` reports it), and a
    negative rc means the process never ran - executable missing, killed by a
    signal - which is a genuine fault rather than a negative answer.
    """
    if not looks_like_exec_result(res):
        return False
    if res.get("timed_out"):
        return False
    rc = _rc_of(res)
    if rc is None or rc <= 0:
        return False
    return not (_txt(res.get("stdout")).strip() or _txt(res.get("stderr")).strip())


def annotate(res: Any, command: Any = "") -> Any:
    """Return `res` with the command and any applicable note attached.

    Three things can be added: the COMMAND (so a failure can name the call it
    came from), the silent-SUCCESS note, and the silent-FAILURE note. Never
    mutates the caller's dict, and never overwrites an existing `note` or
    `command` - a caller that already explained itself knows more about the
    specific command than this does.
    """
    # Anything that is not an exec result dict passes straight through. The
    # previous version got this for free because is_silent_success() was the
    # first thing it called; doing work before that check reintroduced it, and
    # test_a_non_exec_result_is_never_touched caught it with a list.
    if not looks_like_exec_result(res):
        return res

    add: Dict[str, Any] = {}

    # The command, so a failure can name the call it came from. Without this
    # `result_failure_reason` has nothing to identify an exec failure by, and
    # its "name the command" branch - written precisely so the reader is "not
    # reduced to guessing which call this was" - is dead code for every exec
    # capability. Attached on success too: it costs one field and makes a
    # result readable on its own.
    cmd = _txt(command).strip()
    if cmd and not _txt(res.get("command")).strip():
        add["command"] = cmd[:MAX_COMMAND]

    # Never overwrite an existing note - a caller that already explained itself
    # knows more about the specific command than this does.
    if not _txt(res.get("note")).strip():
        if is_silent_success(res):
            add["note"] = NOTE
        elif is_silent_failure(res):
            add["note"] = FAILURE_NOTE.format(rc=_rc_of(res))

    if not add:
        return res
    out = dict(res)
    out.update(add)
    return out
