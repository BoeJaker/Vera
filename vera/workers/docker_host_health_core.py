"""
docker_host_health_core.py
==========================
Pure logic behind docker.host.health / docker.host.fix_stuck_execs: parse the host's process table, find `runc exec`
helpers that deadlocked, measure dockerd's memory trend, and judge the daemon's health. No I/O - the capability
module gathers the facts and hands them here (tests/test_docker_host_health_core.py).

Why it exists (2026-09-29): dockerd leaked ~0.75 GiB a day for two weeks, reached 14 GiB and was OOM-killed at
01:34 during the nightly backup; systemd restarted it, and the new daemon hung for 16 hours stopping two chat
sandboxes whose `docker exec` had deadlocked in `runc init` 11 and 16 days earlier. Every container was down all
day. Each of those facts is something this can see ahead of time.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

RUNC_ROOT = "/var/run/docker/runtime-runc/moby"
_CID = re.compile(r"/moby/([0-9a-f]{64})/")

# thresholds (the capability lets a caller override them)
STUCK_EXEC_MIN_AGE_S = 3600          # a healthy exec's runc returns in seconds; an hour is certainly stuck
ACTIVATING_ERROR_S = 300             # dockerd "activating" this long never finished starting
RSS_WARN_GIB = 6.0
RSS_ERROR_GIB = 10.0
GROWTH_WARN_GIB_PER_DAY = 0.25


def parse_ps(text: str) -> List[Dict[str, Any]]:
    """Rows of `ps -eo pid=,ppid=,etimes=,stat=,args=` -> [{pid, ppid, age_s, stat, args}]."""
    out = []
    for line in (text or "").splitlines():
        parts = line.split(None, 4)
        if len(parts) < 5 or not parts[0].isdigit() or not parts[1].isdigit():
            continue
        try:
            age = int(parts[2])
        except ValueError:
            continue
        out.append({"pid": int(parts[0]), "ppid": int(parts[1]), "age_s": age, "stat": parts[3], "args": parts[4]})
    return out


def is_runc_exec(args: str) -> bool:
    return args.startswith("runc ") and f"--root {RUNC_ROOT}" in args and " exec " in f" {args} " and "--process" in args


def find_stuck_execs(procs: Sequence[Dict[str, Any]], min_age_s: int = STUCK_EXEC_MIN_AGE_S) -> List[Dict[str, Any]]:
    """A `runc exec` helper older than min_age_s, with the `runc init` it spawned: the deadlock that blocked
    dockerd's restart. Anything else - a young exec, a runc that is not an exec, a process whose shape differs
    - is left out."""
    kids: Dict[int, List[Dict[str, Any]]] = {}
    for p in procs:
        kids.setdefault(p["ppid"], []).append(p)
    found = []
    for p in procs:
        if not is_runc_exec(p["args"]) or p["age_s"] < min_age_s:
            continue
        inits = [k for k in kids.get(p["pid"], []) if k["args"].strip() == "runc init"]
        m = _CID.search(p["args"])
        found.append({"runc_pid": p["pid"], "init_pids": [k["pid"] for k in inits], "shim_pid": p["ppid"],
                      "age_s": p["age_s"], "container_id": m.group(1) if m else "",
                      "age_h": round(p["age_s"] / 3600, 1)})
    return sorted(found, key=lambda s: -s["age_s"])


def kill_targets(stuck: Iterable[Dict[str, Any]]) -> List[int]:
    """The pids a fix kills: each stuck exec's `runc init` children first, then the `runc exec` itself. Never the
    shim, never the container's own processes."""
    pids: List[int] = []
    for s in stuck:
        pids.extend(int(x) for x in s.get("init_pids", []))
        pids.append(int(s["runc_pid"]))
    return pids


def growth_gib_per_day(samples: Sequence[Tuple[float, float]]) -> Optional[float]:
    """Least-squares slope of (unix_ts, rss_gib) in GiB/day; None until the samples span an hour. Samples from
    before a daemon restart must be dropped by the caller (a restart resets the memory)."""
    pts = [(float(t), float(v)) for t, v in samples if t and v is not None]
    if len(pts) < 3 or pts[-1][0] - pts[0][0] < 3600:
        return None
    n = len(pts)
    mt = sum(t for t, _ in pts) / n
    mv = sum(v for _, v in pts) / n
    den = sum((t - mt) ** 2 for t, _ in pts)
    if not den:
        return None
    slope = sum((t - mt) * (v - mv) for t, v in pts) / den       # GiB per second
    return round(slope * 86400, 3)


def assess(state: Dict[str, Any], rss_gib: Optional[float], growth: Optional[float],
           stuck: Sequence[Dict[str, Any]], oom_adj: Optional[int] = None, live_restore: Optional[bool] = None,
           rss_warn: float = RSS_WARN_GIB, rss_error: float = RSS_ERROR_GIB,
           growth_warn: float = GROWTH_WARN_GIB_PER_DAY) -> Dict[str, Any]:
    """state: {active, sub, since_s}. Returns {status: ok|warn|error, findings:[{level, code, message}]}."""
    f: List[Dict[str, str]] = []
    active, sub, since = state.get("active", ""), state.get("sub", ""), state.get("since_s")
    if active == "activating" and since is not None and since >= ACTIVATING_ERROR_S:
        f.append({"level": "ERROR", "code": "daemon_stuck_starting",
                  "message": f"dockerd has been starting for {int(since // 60)} min ({sub}) - containers are down; "
                             f"a hung `runc exec` is the usual cause (docker.host.fix_stuck_execs)"})
    elif active and active not in ("active", "activating"):
        f.append({"level": "ERROR", "code": "daemon_down", "message": f"dockerd is {active} ({sub})"})
    if stuck:
        oldest = stuck[0]
        f.append({"level": "WARNING", "code": "stuck_execs",
                  "message": f"{len(stuck)} `docker exec` helper(s) deadlocked in runc - the oldest {oldest['age_h']} h "
                             f"(container {oldest['container_id'][:12]}); they block the daemon's next restart"})
    if rss_gib is not None:
        if rss_gib >= rss_error:
            f.append({"level": "ERROR", "code": "daemon_memory", "message": f"dockerd holds {rss_gib:.1f} GiB - it was OOM-killed at 14 GiB on 2026-09-29"})
        elif rss_gib >= rss_warn:
            f.append({"level": "WARNING", "code": "daemon_memory", "message": f"dockerd holds {rss_gib:.1f} GiB (warn at {rss_warn:g})"})
    if growth is not None and growth >= growth_warn:
        f.append({"level": "WARNING", "code": "daemon_leak",
                  "message": f"dockerd is growing {growth:.2f} GiB/day - at that rate it reaches {rss_error:g} GiB in "
                             f"{max(0.0, (rss_error - (rss_gib or 0)) / growth):.0f} days"})
    if oom_adj is not None and oom_adj > -500:
        f.append({"level": "WARNING", "code": "daemon_oom_unprotected", "message": f"dockerd's oom_score_adj is {oom_adj}: the OOM killer may pick it first"})
    if live_restore is False:
        f.append({"level": "WARNING", "code": "no_live_restore", "message": "live-restore is off: a dockerd restart stops every container"})
    status = "error" if any(x["level"] == "ERROR" for x in f) else ("warn" if f else "ok")
    return {"status": status, "findings": f}
