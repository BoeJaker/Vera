"""Why a capability result that looks like a failure actually failed.

The generic failure check knows the SHELL-COMMAND shape - rc, stdout, stderr.
Capabilities that fail without ever running a command name their fields
differently, and every time one of those field names is missed the loop hands
the model the string ``failed with no error detail`` while the actual cause sits
in the dict, one key away.

This has now been fixed twice by widening the list, which is why the list lives
here with the evidence attached rather than inline at the callsite:

  * HTTP-shaped results (``status``/``body``/``text``/``message``) - a non-2xx
    reported as ``command failed (rc=0)``, where rc=0 additionally reads as
    SUCCESS to anyone skimming.
  * ``reason`` - census run 16 (2026-08-30), goal ``build-browser-verified``.
    ``operator.run`` returns ``{ok, done, reason, summary, run_id,
    screenshots, step_count}`` and states why it stopped in ``reason``. Two
    browser runs took 478s and 242s of gated GPU time, ended with a populated
    reason, and the model was told only ``failed with no error detail (rc=0,
    keys=[...])``. It then re-ran the same operator call. Twelve minutes of the
    estate's scarcest resource spent, and the one sentence explaining the
    outcome was dropped on the floor.

``summary`` is deliberately last: it is a narrative of what a run DID rather
than a statement of what went wrong, so it is worth showing when nothing else
exists and misleading when it displaces a real error.

A third widening, census 34 (2026-09-05), goal ``research-report``::

    exec.bash.run FAILED - failed with no error detail (rc=92, keys=[
      'elapsed_ms', 'ok', 'rc', 'sandboxed', 'stderr', 'stdout', 'timed_out'])

``timed_out`` is sitting in that key list. A command that was killed for
running too long HAS a cause, and it is a boolean rather than a string, so
every text field is legitimately empty and the model is told nothing. The
fields above answer "what did it say"; FLAG_CAUSES answers "what happened to
it", which is the only thing a silent kill can report.
"""

from __future__ import annotations

from typing import Any, Dict

#: Checked in order. Earlier fields are more specific statements of the error;
#: later ones are progressively more general places a cause might be recorded.
BODY_FIELDS = ("stderr", "error", "stdout", "body", "text", "message",
               "reason", "detail")

#: Only consulted when every BODY_FIELD is empty.
NARRATIVE_FIELDS = ("summary", "note")

#: Boolean fields that ARE the cause when set. A killed command has no stderr
#: to offer, so without these it reports as "no error detail" - which reads as
#: "we have no idea" when in fact we knew exactly what happened.
FLAG_CAUSES = (
    ("timed_out", "the command did not finish in time and was killed - it needs "
                  "to exit on its own, or run in the background"),
    ("killed", "the command was killed before it finished"),
    ("cancelled", "the call was cancelled before it finished"),
    ("truncated", "the output was truncated, so the result is incomplete"),
)

MAX_BODY = 600
MAX_HTTP_BODY = 400
MAX_NARRATIVE = 400


def _first_text(res: Dict[str, Any], fields) -> str:
    for f in fields:
        v = res.get(f)
        if v in (None, "", [], {}):
            continue
        text = str(v).strip()
        if text:
            return text
    return ""


def failure_reason(res: Dict[str, Any], rc: Any) -> str:
    """One line the model can act on, from a result dict that reports failure."""
    if not isinstance(res, dict):
        return f"failed with no error detail (rc={rc})"

    status = res.get("status") or res.get("status_code")
    body = _first_text(res, BODY_FIELDS)
    if status:
        return (f"HTTP {status}: {body[:MAX_HTTP_BODY]}" if body
                else f"HTTP {status} (no response body)")
    if body:
        return body[:MAX_BODY]

    narrative = _first_text(res, NARRATIVE_FIELDS)
    if narrative:
        return narrative[:MAX_NARRATIVE]

    # No text anywhere. Before giving up, ask what HAPPENED to the call - a
    # kill or a timeout is a cause even when nothing was written.
    for field, explanation in FLAG_CAUSES:
        if res.get(field):
            return f"{explanation} (rc={rc})"

    # Genuinely nothing. Name the command if we have it, so the next reader is
    # not reduced to guessing which call this was.
    what = _first_text(res, ("command", "cmd", "path", "url"))
    where = f" [{what[:120]}]" if what else ""
    return (f"failed with no error detail (rc={rc}{where}, "
            f"keys={sorted(str(k) for k in res.keys())[:8]})")
