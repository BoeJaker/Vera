"""Error recovery may reshape WHAT a call asks for, never WHO or WHERE it asks.

The loop's recovery sub-cycle rebuilds a failed call from its own LLM
answer. In census run70-73 (24 Sep 2026) that answer changed a browser
run's `kind` from the sandbox preview to `live` (prod's own UI at
localhost:8994, ERR_EMPTY_RESPONSE), set `allowlist: ["*"]` and
`allow_destructive: true`, invented providers (`playwright`,
`local-sandbox`: "unknown provider", the attempt dead) and models (now
dropped by the model heal). 5 of 18 attempts recovered; the failures were
of this kind.

The rule: fields that say WHERE the call runs and WHAT IT MAY DO are the
original call's - kind, target, base_url, provider, allowlist,
allow_destructive, dry_run, keep_open, session ids, the url's host - and a
recovery answer cannot change them. The goal, selectors, steps and
budgets may change; that is what recovery is for. Pure: dicts in, a dict
and notes out.
"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple
from urllib.parse import urlsplit

#: Fields a recovery answer may not change when the original call set them.
IDENTITY_FIELDS = ("kind", "target", "base_url", "provider", "allowlist", "allow_destructive",
                   "dry_run", "keep_open", "session_id", "sandbox_session", "panel_id", "branch", "id")
#: Fields a recovery answer may not INTRODUCE when the original did not set them.
PERMISSION_FIELDS = ("allow_destructive", "allowlist", "kind", "provider")


#: A call that RAN and stopped on its own limit is not an argument error: the
#: browser's time budget, no-progress and repeating-action stops, an exec wall
#: cap. run77 long-horizon (25 Sep 2026): operator.run stopped on its 511 s
#: budget, and the recovery sub-cycle re-ran the browser twice more inside the
#: same tool call - 1,554 s for one call, then the step's browser budget refused
#: the next. The stop text mentions "expected"/"got", which read as a schema error.
RUN_STOP_MARKERS = (
    "time_budget", "no_progress", "repeating_action", "too_many_errors",
    "stopped after", "wall cap", "wall-cap", "budget is spent", "time limit",
    "having taken", "step(s) without reaching",
)
#: Caps whose one call is a long run of its own (a browser session): never re-run by recovery.
LONG_RUN_TOOLS = ("operator.run", "operator.act", "operator.step", "operator.observe")


def is_run_stop(error_text: object) -> bool:
    """True when the error says the call ran and stopped on a limit - not fixable by other args."""
    e = str(error_text or "").lower()
    return any(m in e for m in RUN_STOP_MARKERS)


def is_long_run_tool(tool: object) -> bool:
    t = str(tool or "")
    return t in LONG_RUN_TOOLS or t.startswith("operator.")


def _host(url: object) -> str:
    try:
        return (urlsplit(str(url or "")).netloc or "").lower()
    except Exception:
        return ""


def keep_identity(original: Dict[str, Any], recovered: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    """`recovered` with the original call's identity restored; the notes say what was put back."""
    out = dict(recovered or {})
    notes: List[str] = []
    orig = original or {}
    for f in IDENTITY_FIELDS:
        if f in orig and orig.get(f) not in (None, "", [], {}):
            if f in out and out.get(f) != orig.get(f):
                notes.append(f"{f}: recovery changed it to {str(out.get(f))[:60]!r} - kept the original {str(orig.get(f))[:60]!r}")
            out[f] = orig[f]
        elif f in PERMISSION_FIELDS and f in out and out.get(f) not in (None, "", [], {}, False):
            notes.append(f"{f}: recovery introduced {str(out.get(f))[:60]!r} - the original call had none; dropped")
            out.pop(f, None)
    # the url may change within the same host (another page of the same target), not to another host
    ou, ru = str(orig.get("url") or ""), str(out.get("url") or "")
    if ou and ru and _host(ou) and _host(ru) != _host(ou):
        notes.append(f"url: recovery moved it to {_host(ru) or ru[:40]!r} - kept the original host")
        out["url"] = ou
    if ou and ru and ru.startswith("file:"):
        notes.append("url: recovery pointed at a file: URL - kept the original")
        out["url"] = ou
    return out, notes
