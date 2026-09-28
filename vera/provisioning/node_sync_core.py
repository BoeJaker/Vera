"""
node_sync_core.py — keep every node worker on the commit the host runs (no app imports)

A node worker runs the host's own commit, shipped by provision.worker. Until
this module, nothing re-shipped it: every promotion to main left the three
nodes on the previous commit until someone re-provisioned them by hand.

The sync job (components_capabilities, `nodes.workers.sync`) runs on the host
and asks this module one question per tick: which node, if any, to refresh now.
The answer is deliberately conservative:

  * compare against the commit the host is RUNNING, read once at import - not
    git HEAD at call time. A promotion to main moves HEAD before the restart
    that activates it; syncing to HEAD then would put nodes ahead of the host.
  * never while a census goal is in flight (the install puts pip on the CPU
    nodes the census uses), and never a node whose worker is mid-task (the
    restart would kill it and orphan recovery would run it again).
  * one node per tick, and a node that failed waits out a backoff before it
    is tried again - no retry loop against a node that is down.

Pure: dicts in, a plan out. Tested without nodes.
"""
from __future__ import annotations

import os
import time
from typing import Any, Dict, Iterable, List, Optional

REGISTRY_KEY = "vera:node_workers"          # hash: host_id -> json record
CONFIG_KEY = "vera:node_workers:cfg"         # json: {"enabled": bool}
TICK_S = 600                                 # scheduler interval
FIRST_TICK_DELAY_S = 120                     # after a boot, let the host settle
BACKOFF_S = (900, 1800, 3600, 7200)          # after 1, 2, 3, 4+ consecutive failures


def read_git_head(repo: str) -> str:
    """The commit a checkout has out, read from .git directly (no subprocess -
    this runs at import). '' when it cannot be read."""
    try:
        gitdir = os.path.join(repo, ".git")
        if os.path.isfile(gitdir):                       # a linked worktree
            with open(gitdir, encoding="utf-8") as fh:
                line = fh.read().strip()
            if line.startswith("gitdir:"):
                gitdir = os.path.join(repo, line[len("gitdir:"):].strip())
        with open(os.path.join(gitdir, "HEAD"), encoding="utf-8") as fh:
            head = fh.read().strip()
        if not head.startswith("ref:"):
            return head if len(head) == 40 else ""
        ref = head[len("ref:"):].strip()
        common = gitdir
        cd = os.path.join(gitdir, "commondir")
        if os.path.isfile(cd):
            with open(cd, encoding="utf-8") as fh:
                common = os.path.normpath(os.path.join(gitdir, fh.read().strip()))
        for base in (gitdir, common):
            p = os.path.join(base, ref)
            if os.path.isfile(p):
                with open(p, encoding="utf-8") as fh:
                    sha = fh.read().strip()
                return sha if len(sha) == 40 else ""
        packed = os.path.join(common, "packed-refs")
        if os.path.isfile(packed):
            with open(packed, encoding="utf-8") as fh:
                for line in fh:
                    parts = line.strip().split(" ", 1)
                    if len(parts) == 2 and parts[1] == ref and len(parts[0]) == 40:
                        return parts[0]
    except OSError:
        pass
    return ""


def backoff_for(failures: int) -> int:
    if failures <= 0:
        return 0
    return BACKOFF_S[min(failures, len(BACKOFF_S)) - 1]


def plan(host_commit: str, entries: Iterable[Dict[str, Any]],
         workers: Iterable[Dict[str, Any]], *, census_busy: bool,
         now: Optional[float] = None, limit: int = 1) -> Dict[str, Any]:
    """What to refresh this tick.

    entries  - registry records: {host_id, nodename, commit, failures,
               last_attempt}
    workers  - live node-worker registrations: {host, status, commit}
    Returns {run: [host_id...], skipped: [{host_id, why}], up_to_date: [...],
    blocked: why-or-''}."""
    now = time.time() if now is None else now
    out: Dict[str, Any] = {"run": [], "skipped": [], "up_to_date": [], "blocked": ""}
    if not host_commit:
        out["blocked"] = "the host's running commit is unknown"
        return out
    if census_busy:
        out["blocked"] = "a census goal is in flight"
        return out
    live = {}
    for w in workers or ():
        if str(w.get("role") or "") == "node-worker" and w.get("host"):
            live[str(w["host"])] = w
    for e in sorted(entries or (), key=lambda e: str(e.get("host_id") or "")):
        hid = str(e.get("host_id") or "")
        if not hid:
            continue
        w = live.get(str(e.get("nodename") or ""))
        # the worker's own report is the truth; the registry is what we shipped
        running = str((w or {}).get("commit") or e.get("commit") or "")
        if w and running == host_commit:
            out["up_to_date"].append(hid)
            continue
        wait = backoff_for(int(e.get("failures") or 0)) - (now - float(e.get("last_attempt") or 0))
        if wait > 0:
            out["skipped"].append({"host_id": hid, "why": "backing off %ds after %d failure(s)"
                                   % (int(wait), int(e.get("failures") or 0))})
            continue
        if w and str(w.get("status") or "").startswith("running"):
            out["skipped"].append({"host_id": hid, "why": "worker is running %s"
                                   % str(w.get("status"))[len("running:"):]})
            continue
        if len(out["run"]) < max(1, int(limit)):
            out["run"].append(hid)
        else:
            out["skipped"].append({"host_id": hid, "why": "one node per tick"})
    return out


def record_after(entry: Dict[str, Any], *, ok: bool, commit: str = "", error: str = "",
                 nodename: str = "", now: Optional[float] = None) -> Dict[str, Any]:
    """The registry record after an attempt."""
    now = time.time() if now is None else now
    e = dict(entry or {})
    e["last_attempt"] = now
    if ok:
        e.update(commit=commit, failures=0, last_error="", provisioned_at=now)
        if nodename:
            e["nodename"] = nodename
    else:
        e["failures"] = int(e.get("failures") or 0) + 1
        e["last_error"] = str(error or "")[:300]
    return e
