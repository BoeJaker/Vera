"""How many files the server may hold open.

Pure (no app imports) so it is testable from a worktree as `vera.proc_limits_core`.

2026-10-06 (shared planning context-and-ui E30): prod ran with the shell's soft limit of 1024 open files - kept across
every in-place re-exec - against a hard limit of 1048576. 936 Redis connections took it to 1024, and from then on
every accept, file read and database connect failed with "Too many open files"; the process stayed up and answered
nothing for nine hours. The soft limit is raised at start-up to a level that turns a slow leak into a warning on a
dashboard rather than an outage; it is never lowered and never raised past the hard limit.
"""
from __future__ import annotations

from typing import Optional, Tuple

WANT = 65536


def nofile_target(soft: int, hard: int, want: int = WANT) -> Optional[int]:
    """The soft limit to set, or None when the current one is already at least `want` (or the hard limit)."""
    unlimited = hard is None or hard < 0
    goal = want if unlimited else min(want, hard)
    if soft is not None and soft >= 0 and soft >= goal:
        return None
    return goal


def raise_nofile(resource_mod=None, want: int = WANT) -> Tuple[int, int]:
    """Raise this process's soft open-files limit towards `want`; (before, after). Never raises."""
    try:
        res = resource_mod
        if res is None:
            import resource as res  # type: ignore
        soft, hard = res.getrlimit(res.RLIMIT_NOFILE)
        to = nofile_target(soft, hard if hard != res.RLIM_INFINITY else -1, want)
        if to is None:
            return soft, soft
        res.setrlimit(res.RLIMIT_NOFILE, (to, hard))
        return soft, res.getrlimit(res.RLIMIT_NOFILE)[0]
    except Exception:
        return -1, -1
