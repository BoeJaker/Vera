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


WARN_SHARE = 0.25


def files_status(open_now: int, soft: int, redis_conns: int = -1, warn_share: float = WARN_SHARE) -> dict:
    """What /health says about open files: {open, limit, share, redis_conns, warn}. `warn` is True from
    `warn_share` of the limit - early enough that a leak like 2026-10-06's (~110 Redis connections an hour) is seen
    days before it stops the server. Unknown values are -1 and never warn."""
    limit = int(soft) if soft and int(soft) > 0 else -1
    n = int(open_now) if open_now is not None and int(open_now) >= 0 else -1
    share = round(n / limit, 3) if n >= 0 and limit > 0 else -1.0
    return {"open": n, "limit": limit, "share": share,
            "redis_conns": int(redis_conns) if redis_conns is not None else -1,
            "warn": bool(share >= 0 and share >= warn_share)}