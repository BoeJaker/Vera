"""
sandbox_pool.py — pure per-branch dev-sandbox allocation (unit-testable, no docker)
==================================================================================

Turns a branch name into a docker-safe container slug, and allocates a free host
port + Redis DB for that branch's container from bounded pools, skipping whatever
is already in use. This is what lets the single dev sandbox (one `vera-dev` on
8998 / db 3) generalize to MANY concurrent per-branch containers
(dev-lifecycle-and-repo-hygiene.md §4). Stdlib-only, no side effects — same pattern
as dag/planner_core.py, ide/ws_changes_core.py, evolve/evolve_git_core.py.
"""

from __future__ import annotations

import re

# 8999 = prod (reserved). Primary dev historically = 8998. The pool spans DOWN
# from 8998 so the first sandbox keeps its familiar port; 8980 floor leaves room
# for other services below.
PORT_POOL = list(range(8998, 8979, -1))     # [8998, 8997, …, 8980], high-first
RESERVED_PORTS = {8999}
# Redis DBs 0–2 are prod / other subsystems; the dev pool is 3–15.
DB_POOL = list(range(3, 16))


def slug_for_branch(branch: str) -> str:
    """A docker-safe container-name slug from a branch name: lowercased, '/'→'-',
    reduced to [a-z0-9._-], collapsed, trimmed, and capped. Docker names must match
    [a-zA-Z0-9][a-zA-Z0-9_.-]* — and we keep it short and readable."""
    s = (branch or "").strip().lower().replace("/", "-")
    s = re.sub(r"[^a-z0-9._-]+", "-", s)
    s = re.sub(r"[-_.]{2,}", "-", s).strip("-._")
    return (s or "dev")[:40].strip("-._") or "dev"


def container_name(branch: str) -> str:
    """The dev container name for a branch, e.g. feat/x -> 'vera-dev-feat-x'."""
    return f"vera-dev-{slug_for_branch(branch)}"


def alloc_port(in_use, pool=PORT_POOL, reserved=RESERVED_PORTS):
    """Highest free port in `pool` that is neither `in_use` nor reserved; None if
    the pool is exhausted (caller should surface 'no free dev port')."""
    used = {int(p) for p in (in_use or [])}
    res = {int(p) for p in (reserved or ())}
    for p in pool:
        if p not in used and p not in res:
            return p
    return None


def alloc_db(in_use, pool=DB_POOL):
    """Lowest free Redis DB in `pool` not in `in_use`; None if exhausted."""
    used = {int(d) for d in (in_use or [])}
    for d in pool:
        if d not in used:
            return d
    return None
