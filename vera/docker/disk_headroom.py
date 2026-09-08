"""Say the docker disk is filling BEFORE it fills, and name what to reap.

On 2026-09-08 `/mnt/dockerdata` reached **0 bytes free** and took the estate
down with it. Postgres crashed and could not complete recovery because it could
not write; Neo4j refused to start with `java.io.IOException: No space left on
device`. Neither failure named the disk — Vera reported them as "cannot connect
to postgres / neo4j", and `obs.health` reported BOTH as `true` throughout,
because it proves a port is open rather than that the database answers.

Nothing warned. The disk went from working to full with no signal in between.

WHAT FILLED IT
490 exited `vera-sbx-*` session sandboxes, the oldest 48 days old. A loop run
creates one and leaves the container behind on exit; a 12-goal census leaves
twelve. They are never reaped, so the only question was when the disk would run
out, not whether.

TWO THRESHOLDS AND A GUARD, BECAUSE EITHER ALONE LIES
A percentage alone is useless across disk sizes — 85% of a 5TB array leaves
750G, which is nothing to wake anyone for. An absolute alone is worse: a 20G
volume with 19G free is 5% used and perfectly healthy, yet trips any fixed
"under 20G free" floor.

So: a level fires when either bound is crossed, but ONLY on a disk that is
already substantially used (`MIN_PCT_TO_CONSIDER`). That guard is what makes
the absolute floor meaningful — it cannot fire on a disk that is mostly empty,
whatever its size. The first version of this module had no guard and warned
about a 5%-used volume; the test for it is
`test_an_absolute_alone_would_miss_a_small_disk_running_out`.

The message carries both numbers so the reader can judge rather than trust.

Pure: a filesystem reading and a container list in, a verdict and a reap list
out. Nothing here deletes anything.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional, Sequence

#: Warn while there is still time to act; alarm when action is overdue.
#: Chosen against the real incident: the disk sat around 87% for a long period
#: (the root filesystem still reads 87% today) and that is not an emergency,
#: while the last 5% vanished fast enough that nobody caught it.
WARN_PCT_USED = 85.0
CRIT_PCT_USED = 95.0

#: Absolute floors, for the same reason in the other direction. 20G is roughly
#: what this estate needs to survive one Postgres recovery plus a few image
#: pulls; below 8G a single container build can finish the job.
WARN_FREE_GB = 20.0
CRIT_FREE_GB = 8.0

#: A disk this empty is not running out, whatever its absolute free space says.
#: Without this guard the floors above fire on a 20G volume with 19G free.
MIN_PCT_TO_CONSIDER = 50.0

OK, WARN, CRITICAL = "ok", "warn", "critical"

#: Session sandboxes: one per loop run, left behind on exit. This prefix is
#: what distinguishes them from the Loop Lab's own dev sandboxes
#: (`vera-dev-*`), which are owned by agents and must NEVER be reaped here.
SESSION_PREFIX = "vera-sbx-"

#: Keep anything that finished within a day. Set from the operator's constraint
#: during the incident — "be careful not to stop anything being used in the last
#: 24hrs" — and it is the right default anyway: a session container that exited
#: an hour ago may still be being read.
DEFAULT_RETAIN_HOURS = 24.0


def _num(v: Any) -> Optional[float]:
    try:
        f = float(v)
        return f if f == f else None            # reject NaN
    except (TypeError, ValueError):
        return None


def level(total_gb: Any, free_gb: Any) -> str:
    """How bad is it? Either bound crossing is enough to fire."""
    t, f = _num(total_gb), _num(free_gb)
    if t is None or f is None or t <= 0 or f < 0:
        return OK                                # unreadable is not an alarm
    pct_used = 100.0 * (t - f) / t
    if pct_used < MIN_PCT_TO_CONSIDER:
        return OK                                # mostly empty; not running out
    if pct_used >= CRIT_PCT_USED or f <= CRIT_FREE_GB:
        return CRITICAL
    if pct_used >= WARN_PCT_USED or f <= WARN_FREE_GB:
        return WARN
    return OK


def describe(mount: str, total_gb: Any, free_gb: Any) -> Dict[str, Any]:
    """The verdict plus both numbers, so a reader can judge rather than trust."""
    t, f = _num(total_gb), _num(free_gb)
    lv = level(total_gb, free_gb)
    if t is None or f is None or t <= 0:
        return {"mount": mount, "level": OK, "readable": False,
                "note": "could not read %s" % mount}
    pct_used = round(100.0 * (t - f) / t, 1)
    note = "%s: %.0fG free of %.0fG (%.1f%% used)" % (mount, f, t, pct_used)
    if lv == CRITICAL:
        note += " — CRITICAL: writes will start failing. Postgres cannot " \
                "finish crash recovery and Neo4j will not start on a full disk."
    elif lv == WARN:
        note += " — low. Reap exited session sandboxes before it bites."
    return {"mount": mount, "level": lv, "readable": True,
            "total_gb": round(t, 1), "free_gb": round(f, 1),
            "pct_used": pct_used, "note": note}


def parse_df_line(line: str) -> Optional[Dict[str, Any]]:
    """One `df -PBG` row -> {mount, total_gb, free_gb}.

    -P (POSIX) keeps each filesystem on ONE line; without it a long device name
    wraps and the columns shift, which is how a parser like this silently starts
    reading the wrong field.
    """
    parts = str(line or "").split()
    if len(parts) < 6 or parts[0] == "Filesystem":
        return None
    try:
        total = float(re.sub(r"[^0-9.]", "", parts[1]))
        free = float(re.sub(r"[^0-9.]", "", parts[3]))
    except (ValueError, IndexError):
        return None
    return {"mount": parts[5], "total_gb": total, "free_gb": free}


def reapable(containers: Optional[Iterable[Dict[str, Any]]],
             retain_hours: float = DEFAULT_RETAIN_HOURS) -> List[Dict[str, Any]]:
    """Which session sandboxes may be removed.

    Each container is {name, running, finished_hours_ago}. A container is
    reapable only when ALL of these hold:

      * its name carries the session prefix — a Loop Lab dev sandbox
        (`vera-dev-*`) belongs to an agent and is never reaped here;
      * it is not running;
      * we KNOW when it finished, and that was longer ago than the retention.

    The unknown-age case is deliberately a keep. An exited container with an
    unreadable timestamp is exactly the one worth being careful about, and a
    reaper that guesses is worse than a full disk.
    """
    out: List[Dict[str, Any]] = []
    for c in (containers or []):
        if not isinstance(c, dict):
            continue
        name = str(c.get("name") or "")
        if not name.startswith(SESSION_PREFIX):
            continue
        if c.get("running"):
            continue
        age = _num(c.get("finished_hours_ago"))
        if age is None or age <= float(retain_hours):
            continue
        out.append({"name": name, "id": str(c.get("id") or ""),
                    "finished_hours_ago": round(age, 1)})
    return out


def reap_summary(containers: Optional[Sequence[Dict[str, Any]]],
                 retain_hours: float = DEFAULT_RETAIN_HOURS) -> Dict[str, Any]:
    """What a reap would do, in words, without doing it."""
    all_c = [c for c in (containers or []) if isinstance(c, dict)]
    sess = [c for c in all_c if str(c.get("name") or "").startswith(SESSION_PREFIX)]
    running = [c for c in sess if c.get("running")]
    doomed = reapable(sess, retain_hours)
    kept = len(sess) - len(running) - len(doomed)
    return {
        "session_total": len(sess),
        "running_kept": len(running),
        "recent_kept": kept,
        "reapable": len(doomed),
        "retain_hours": float(retain_hours),
        "names": [d["name"] for d in doomed],
        "note": ("%d exited session sandbox(es) older than %.0fh; keeping %d "
                 "running and %d that finished recently"
                 % (len(doomed), float(retain_hours), len(running), kept)),
    }
