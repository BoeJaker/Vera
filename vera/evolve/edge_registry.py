"""
edge_registry.py - the named integration branches Loop Lab lands on ("bleeding edges")
====================================================================================

Until now Loop Lab knew ONE integration branch, `bleeding-edge`, spelled out as a
literal in a dozen places: the pipeline base, the adopt/promote target, the
standing container, the release-to-main step, the reaper's protected set, the
lifecycle guard, the worktree repair refusal, the git hooks and the panel. A
programme that must land on its OWN trunk (the UI redesign, Notes/41) needs a
second edge, and hacking a second literal into every site is exactly how the
trunk got reaped once (see sandbox_reap.TRUNK_PROTECTED_BRANCHES).

So the edges are a REGISTRY, and every site asks it. An edge record:

    name        the registry key and what a caller passes as edge=
    branch      the integration branch itself (adopt/promote `to=`)
    base        the branch the edge is released to (fast-forward only)
    description one line for the panel
    port        PREFERRED host port for its standing container (0 = allocate)
    redis_db    PREFERRED Redis DB for its standing container (0 = allocate)

Derived, never stored: the mirror branch (`loop-lab/<branch>-mirror`) its standing
container checks out, the pool slug (what evolve_capabilities._safe_branch makes of
the mirror), and the container name. The set of all edge branches + mirrors is the
protected set the reaper, the lifecycle guard and the hooks share; the hooks read
it from tools/hooks/protected-branches, and a unit test keeps that file and this
registry in agreement.

Stdlib-only and side-effect free (same pattern as sandbox_pool.py /
evolve_git_core.py) so it is unit-testable without booting Vera and safe to
import from every evolve module.
"""

from __future__ import annotations

import copy
import os
import re
from typing import Any, Dict, Iterable, List, Optional

# Kept equal to evolve_capabilities.BRANCH_PREFIX (asserted by test_edge_registry).
BRANCH_PREFIX = "loop-lab/"
MAINLINE_MIRROR_BRANCH = "loop-lab/mainline-mirror"

DEFAULT_EDGE = "bleeding-edge"

# name -> record. Order matters for the panel: the default edge first.
_BUILTIN_EDGES: Dict[str, Dict[str, Any]] = {
    "bleeding-edge": {
        "branch": "bleeding-edge",
        "base": "main",
        "description": "the staging trunk every change funnels through before main",
        "port": 8994,
        "redis_db": 4,
    },
    "bleeding-edge-design": {
        "branch": "bleeding-edge-design",
        "base": "main",
        "description": "the UI redesign programme's own trunk (Notes/41); released to main separately",
        "port": 0,
        "redis_db": 0,
    },
}

# Extra edges may be declared without a code change:
#   VERA_EDGES="name=branch[:base],name2=branch2"   e.g. "bleeding-edge-ops=bleeding-edge-ops:main"
_ENV_VAR = "VERA_EDGES"


def _safe(branch: str) -> str:
    """The pool slug evolve_capabilities._safe_branch makes of a branch: the
    loop-lab/ prefix dropped, then anything outside [A-Za-z0-9._-] collapsed to '-'."""
    b = (branch or "").strip()
    if b.startswith(BRANCH_PREFIX):
        b = b[len(BRANCH_PREFIX):]
    return re.sub(r"[^A-Za-z0-9._-]+", "-", b)


def mirror_branch_for(branch: str) -> str:
    """The loop-lab mirror branch a standing container checks out for an edge.
    `bleeding-edge` -> `loop-lab/bleeding-edge-mirror` (the historical name)."""
    return f"{BRANCH_PREFIX}{(branch or '').strip()}-mirror"


def _env_edges(raw: Optional[str]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for item in (raw or "").split(","):
        item = item.strip()
        if not item or "=" not in item:
            continue
        name, spec = item.split("=", 1)
        name = name.strip()
        branch, _, base = spec.strip().partition(":")
        if not name or not branch.strip():
            continue
        out[name] = {"branch": branch.strip(), "base": (base.strip() or "main"),
                     "description": "declared via " + _ENV_VAR, "port": 0, "redis_db": 0}
    return out


def _complete(name: str, rec: Dict[str, Any]) -> Dict[str, Any]:
    r = copy.deepcopy(rec)
    r["name"] = name
    r.setdefault("base", "main")
    r.setdefault("description", "")
    r["port"] = int(r.get("port") or 0)
    r["redis_db"] = int(r.get("redis_db") or 0)
    r["mirror"] = mirror_branch_for(r["branch"])
    r["slug"] = _safe(r["mirror"])
    r["default"] = name == DEFAULT_EDGE
    return r


def edges(env: Optional[Dict[str, str]] = None) -> Dict[str, Dict[str, Any]]:
    """Every registered edge, built-ins first, then any declared in the
    environment (an env entry with a built-in's name overrides its branch/base)."""
    env_map = os.environ if env is None else env
    merged: Dict[str, Dict[str, Any]] = copy.deepcopy(_BUILTIN_EDGES)
    for name, rec in _env_edges(env_map.get(_ENV_VAR, "")).items():
        merged[name] = {**merged.get(name, {}), **rec}
    return {name: _complete(name, rec) for name, rec in merged.items()}


def edge_names(env: Optional[Dict[str, str]] = None) -> List[str]:
    return list(edges(env).keys())


def resolve_edge(name_or_branch: str = "", env: Optional[Dict[str, str]] = None) -> Optional[Dict[str, Any]]:
    """The edge a caller means, by registry name, branch name, mirror branch or
    pool slug; blank means the default edge. None when nothing matches."""
    key = (name_or_branch or "").strip()
    reg = edges(env)
    if not key:
        return reg.get(DEFAULT_EDGE)
    if key in reg:
        return reg[key]
    low = key.lower()
    for rec in reg.values():
        if low in (rec["branch"].lower(), rec["mirror"].lower(), rec["slug"].lower(), rec["name"].lower()):
            return rec
    return None


def edge_for_branch(branch: str, env: Optional[Dict[str, str]] = None) -> Optional[Dict[str, Any]]:
    """The edge whose integration BRANCH is exactly `branch` (case-insensitive),
    else None. This is what promote asks after a merge into `to`: is `to` an edge
    whose standing container must be refreshed?"""
    b = (branch or "").strip().lower()
    if not b:
        return None
    for rec in edges(env).values():
        if rec["branch"].lower() == b:
            return rec
    return None


def is_edge_branch(branch: str, env: Optional[Dict[str, str]] = None) -> bool:
    return edge_for_branch(branch, env) is not None


def is_edge_mirror(branch: str, env: Optional[Dict[str, str]] = None) -> bool:
    b = (branch or "").strip().lower()
    return any(rec["mirror"].lower() == b for rec in edges(env).values())


def edge_protected_branches(env: Optional[Dict[str, str]] = None) -> frozenset:
    """Every branch the sweep, the lifecycle guard and the repair refusal must
    treat as infrastructure: the mainlines, the mainline mirror, and each edge's
    branch and mirror."""
    names = {"main", "master", MAINLINE_MIRROR_BRANCH}
    for rec in edges(env).values():
        names.add(rec["branch"])
        names.add(rec["mirror"])
    return frozenset(names)


def hook_protected_names(env: Optional[Dict[str, str]] = None) -> List[str]:
    """The branch names the git hooks protect (tools/hooks/protected-branches):
    the mainlines plus every edge branch. Mirrors are local-only and not listed."""
    names = ["main", "master"] + [rec["branch"] for rec in edges(env).values()]
    seen: List[str] = []
    for n in names:
        if n not in seen:
            seen.append(n)
    return seen


def read_protected_branches_file(path: str) -> List[str]:
    """The names in a tools/hooks/protected-branches file: one per line, blank
    lines and '#' comments ignored. Empty when the file is absent."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return []
    out: List[str] = []
    for line in lines:
        s = line.split("#", 1)[0].strip()
        if s and s not in out:
            out.append(s)
    return out


def protected_file_drift(file_names: Iterable[str], env: Optional[Dict[str, str]] = None) -> Dict[str, List[str]]:
    """What the hooks file is missing vs. the registry (and what it carries that
    the registry does not) - the pure check behind test_edge_registry."""
    have = {n.strip() for n in file_names if n and n.strip()}
    want = set(hook_protected_names(env))
    return {"missing": sorted(want - have), "extra": sorted(have - want)}
