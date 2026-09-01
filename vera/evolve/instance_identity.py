"""Every estate write names the instance that made it - and silence names the rest.

On 2026-08-31 a stale sandbox container, running code from before 2026-08-30,
spent a day deleting sandbox pool descriptors out from under the live estate. It
took a day to find, and it was only found because `sandbox.prune`'s audit
summary happens to have changed FORMAT between those versions:

    old code : "2 dead pool entr(ies), 2 reconciled (worktree-gone)"
    new code : "pool: nothing to reconcile (2 descriptor(s))"

Classifying the audit by that accident gave the answer immediately - 23
old-format passes, 7 of them destructive; 21 new-format passes, none destructive
- but it was luck. Nothing in an audit entry said WHICH process wrote it, so
"something is reaping the estate" could not be turned into "that one".

THE AWKWARD PART, and the reason this module is shaped the way it is: a stale
instance cannot be made to log better. It runs its own copy of the code and will
never call anything added here. So the design does not try. Instead every write
from CURRENT code carries a `by` stamp, which makes the absence of one
informative:

    an estate write with no `by` stamp was made by code older than 2026-09-01

That turns a day of inference into a single query, and it keeps working no
matter how old the offender is, because it asks nothing of it.

`ver` is the commit the writer is RUNNING, not the commit checked out on disk
next to it - a container holds its own copy of the tree, which is exactly how
the drift arises.
"""

from __future__ import annotations

import os
import socket
import subprocess
from typing import Any, Dict, Iterable, List, Mapping, Optional

#: Audit actions that change shared estate state. An unstamped one of these is
#: the signal worth acting on; an unstamped read is merely old.
ESTATE_ACTIONS = (
    "sandbox.prune", "sandbox.down", "sandbox.reap", "sandbox.pause",
    "sandbox.up", "sandbox.spawn", "branch.delete", "worktree.remove",
    "bleeding_edge.promote_to_main",
)

_CACHED: Optional[Dict[str, Any]] = None


def _git_head(repo_root: str = "") -> str:
    """Short sha of the code this process is running from."""
    try:
        root = repo_root or os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))))
        r = subprocess.run(["git", "-C", root, "rev-parse", "--short", "HEAD"],
                           capture_output=True, text=True, timeout=5)
        return (r.stdout or "").strip()
    except Exception:
        return ""


def identity(refresh: bool = False) -> Dict[str, Any]:
    """Who this process is. Cached - it cannot change without a restart."""
    global _CACHED
    if _CACHED is not None and not refresh:
        return dict(_CACHED)
    dev = str(os.environ.get("VERA_IS_DEV_SANDBOX", "")).strip().lower() in (
        "1", "true", "yes", "on")
    ident = {
        # In a container the hostname IS the container id, which is what makes
        # this traceable back to a specific sandbox.
        "host": socket.gethostname(),
        "pid": os.getpid(),
        "dev": dev,
        "ver": _git_head(),
    }
    _CACHED = dict(ident)
    return dict(ident)


def stamp(entry: Mapping[str, Any]) -> Dict[str, Any]:
    """The audit entry with a `by` stamp added (never overwrites one)."""
    out = dict(entry or {})
    if "by" not in out:
        out["by"] = identity()
    return out


# ── reading it back ─────────────────────────────────────────────────────────

def is_estate_action(action: str) -> bool:
    return str(action or "") in ESTATE_ACTIONS


def writer_key(entry: Mapping[str, Any]) -> str:
    """A stable label for the writer of one entry."""
    by = (entry or {}).get("by")
    if not isinstance(by, Mapping):
        return "UNSTAMPED"
    host = str(by.get("host") or "?")
    ver = str(by.get("ver") or "?")
    return f"{host}@{ver}{' (dev)' if by.get('dev') else ''}"


def classify_writers(entries: Optional[Iterable[Mapping[str, Any]]],
                     current_ver: str = "") -> Dict[str, Any]:
    """Who has been mutating the estate, and which of them should not be.

    `unstamped` is the finding that matters: those entries were written by code
    predating this stamp, i.e. an instance nobody has restarted.
    """
    seen: Dict[str, Dict[str, Any]] = {}
    unstamped = 0
    for e in (entries or []):
        if not isinstance(e, Mapping) or not is_estate_action(e.get("action", "")):
            continue
        key = writer_key(e)
        rec = seen.setdefault(key, {"writer": key, "count": 0, "actions": set(),
                                    "last_ts": "", "stale": False, "unstamped": False})
        rec["count"] += 1
        rec["actions"].add(str(e.get("action") or ""))
        ts = str(e.get("ts") or "")
        if ts > rec["last_ts"]:
            rec["last_ts"] = ts
        if key == "UNSTAMPED":
            rec["unstamped"] = True
            rec["stale"] = True
            unstamped += 1
        else:
            by = e.get("by") or {}
            v = str(by.get("ver") or "")
            if current_ver and v and v != current_ver:
                rec["stale"] = True
    rows = []
    for rec in seen.values():
        rec["actions"] = sorted(rec["actions"])
        rows.append(rec)
    rows.sort(key=lambda r: (not r["stale"], -r["count"]))
    return {"writers": rows, "unstamped": unstamped,
            "stale_writers": [r["writer"] for r in rows if r["stale"]],
            "current_ver": current_ver}


def describe(plan: Mapping[str, Any]) -> str:
    """One line a human can act on."""
    rows = list(plan.get("writers") or [])
    if not rows:
        return "no estate writes recorded"
    stale = [r for r in rows if r.get("stale")]
    if not stale:
        return f"{len(rows)} estate writer(s), all on the current build"
    bits = []
    for r in stale:
        what = ", ".join(r.get("actions") or [])
        if r.get("unstamped"):
            bits.append(f"UNSTAMPED x{r['count']} ({what}) - written by code older "
                        f"than 2026-09-01, i.e. an instance nobody has restarted")
        else:
            bits.append(f"{r['writer']} x{r['count']} ({what})")
    return "STALE ESTATE WRITERS: " + "; ".join(bits)
