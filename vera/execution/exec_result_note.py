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


def annotate(res: Any) -> Any:
    """Return `res` with the note attached when it is a silent success.

    Never mutates the caller's dict, and never overwrites an existing `note` —
    a caller that already explained itself knows more about the specific command
    than this does.
    """
    if not is_silent_success(res) or _txt(res.get("note")).strip():
        return res
    out = dict(res)
    out["note"] = NOTE
    return out
