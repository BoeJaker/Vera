"""
provenance.py — which commit/branch the running Vera process is on
==================================================================

The planner-drift incident took two multi-hour sessions to diagnose largely
because nothing tied a running behaviour (a bad plan, an error, a log line) to
the exact code that produced it: the breaking change was uncommitted and later
bundled into one commit, so "what changed, on which branch, dirty or not?" was
unanswerable (see documentation/postmortems/2026-08-06-agentic-loop-planner-
drift.md and the plan in documentation/specs/dev-lifecycle-and-repo-hygiene.md).

This computes the running process's git provenance ONCE (at first use ≈ boot —
Python modules load at start, so the git state then is the code that's actually
running) and caches it, and stamps a compact copy onto every emitted event via
`event_stamp`. Any event/log/run is then one hop from the commit + branch + a
"was the checkout dirty?" flag. Full detail is exposed by the `obs.provenance`
capability.

Resolution order (first that yields a SHA wins):
  1. Env vars  VERA_GIT_SHA / VERA_GIT_BRANCH / VERA_GIT_DIRTY  — how a deploy
     injects provenance it already knows; wins over everything.
  2. Live `git` in the repo root — works on a dev box / direct run where the
     git binary AND a real .git are present.
  3. A persisted `<repo_root>/.vera-provenance.json` — REQUIRED for containers:
     the app image has no git binary and a worktree's .git points outside the
     bind mount, so git can't resolve in-process. Whatever brings the process
     up (deploy script, `evolve.sandbox.up`) runs `python -m Vera.vera.provenance`
     host-side, where git works; that persists this file into the bind-mounted
     repo root for the container to read. This file is a per-environment
     artifact, not source — it is gitignored.
When live git resolves, it is persisted (2 → writes the file 3 reads) so the
host-side `python -m` invocation is all a bring-up needs.

Dependency-free (stdlib only) and never raises — a provenance failure must never
break event emission.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
from datetime import datetime, timezone
from typing import Any, Dict

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PROV_FILE = os.path.join(_REPO_ROOT, ".vera-provenance.json")
_CACHE: Dict[str, Any] = {}


def _git(args) -> str:
    try:
        out = subprocess.run(
            ["git", *args], cwd=_REPO_ROOT, capture_output=True, text=True,
            timeout=5,
        )
        return (out.stdout or "").strip() if out.returncode == 0 else ""
    except Exception:
        return ""


def _from_env() -> Dict[str, Any]:
    """Provenance a deploy injected via env, or {} if VERA_GIT_SHA is unset."""
    sha = (os.getenv("VERA_GIT_SHA") or "").strip()
    if not sha:
        return {}
    dirty_raw = (os.getenv("VERA_GIT_DIRTY") or "").strip().lower()
    return {
        "git_sha": sha,
        "git_sha_short": sha[:10],
        "branch": (os.getenv("VERA_GIT_BRANCH") or "").strip(),
        "dirty": dirty_raw in ("1", "true", "yes", "dirty"),
        "source": "env",
    }


def _from_git() -> Dict[str, Any]:
    """Provenance from a live `git` invocation, or {} if git can't resolve HEAD
    (no git binary, or a worktree .git pointing outside a container mount)."""
    sha = _git(["rev-parse", "HEAD"])
    if not sha:
        return {}
    return {
        "git_sha": sha,
        "git_sha_short": sha[:10],
        "branch": _git(["rev-parse", "--abbrev-ref", "HEAD"]),
        # `--porcelain` is empty iff clean. Untracked files count as dirty on
        # purpose: uncommitted state is exactly what we want flagged.
        "dirty": bool(_git(["status", "--porcelain"])),
        "source": "git",
    }


def _from_file() -> Dict[str, Any]:
    """Provenance a host-side bring-up persisted into the repo root, or {}."""
    try:
        with open(_PROV_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and data.get("git_sha"):
            data = dict(data)
            data.setdefault("git_sha_short", str(data["git_sha"])[:10])
            data["source"] = "file"
            return data
    except Exception:
        pass
    return {}


def _persist(core: Dict[str, Any]) -> None:
    """Best-effort write of the git-resolved core to the shared file so a
    container (no git) can read what a git-capable run resolved. Atomic (temp +
    os.replace) so a concurrent reader never sees a half-written file. Never
    raises."""
    try:
        payload = {k: core[k] for k in ("git_sha", "git_sha_short", "branch", "dirty")}
        tmp = f"{_PROV_FILE}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        os.replace(tmp, _PROV_FILE)          # atomic on POSIX + Windows
    except Exception:
        pass


def get_provenance() -> Dict[str, Any]:
    """The running process's git provenance, computed once and cached.

    {git_sha, git_sha_short, branch, dirty, source, instance, pid, started_at}.
    All git fields degrade gracefully (empty strings / False) if no source can
    resolve them — this never raises."""
    if _CACHE:
        return dict(_CACHE)
    core = _from_env()
    if not core:
        core = _from_git()
        if core:
            _persist(core)          # let git-less containers read what we found
    if not core:
        core = _from_file()
    if not core:
        core = {"git_sha": "", "git_sha_short": "", "branch": "", "dirty": False,
                "source": "none"}
    core.update({
        "instance": os.getenv("VERA_INSTANCE") or socket.gethostname() or "",
        "pid": os.getpid(),
        "started_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    })
    _CACHE.update(core)
    return dict(_CACHE)


def write_provenance_file(root: str | None = None) -> Dict[str, Any]:
    """Resolve provenance from live git and persist it to <root>/.vera-provenance
    .json. Intended to run HOST-SIDE at bring-up (deploy / evolve.sandbox.up),
    where git works, so the git-less container can read it. Returns the core."""
    global _PROV_FILE
    if root:
        _PROV_FILE = os.path.join(root, ".vera-provenance.json")
    core = _from_git()
    if core:
        _persist(core)
    return core


def event_stamp(event: Dict[str, Any]) -> None:
    """Stamp compact provenance onto an event IN PLACE (via setdefault, so an
    event that already carries these keeps them). Cheap — a cached dict read.
    Never raises: emitting an event must not depend on provenance succeeding."""
    try:
        p = get_provenance()
        event.setdefault("ver", p["git_sha_short"])
        event.setdefault("br", p["branch"])
        event.setdefault("dirty", p["dirty"])
    except Exception:
        pass


if __name__ == "__main__":
    # Host-side bring-up hook: `python -m Vera.vera.provenance` resolves live git
    # and persists .vera-provenance.json for the (git-less) container to read.
    import sys
    core = write_provenance_file(sys.argv[1] if len(sys.argv) > 1 else None)
    print(json.dumps(core or {"git_sha": ""}))
