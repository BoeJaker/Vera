"""The secondary queue: one place that decides when background work may run.

Bulk embedding, dream cycles, narration, source gathering — work nobody is
waiting on. It should run when the box is quiet, get out of the way when it is
not, and never contend with itself. Until now each decided for itself, and the
transcript ingest decided nothing at all: `_scheduled_ingest_all` fired every
300 seconds regardless of what else was happening.

WHAT THAT COST, twice, measured
2026-09-08: a transcript backfill ran straight through two censuses.

    embed, box otherwise idle      ~4.0s   per record
    embed, census running         ~11.1s   per record   (2.8x)

Every ingested turn costs TWO embeds (`data_fabric.py:_embed` and
`memory.py:embed_text`), so a census goal and the backfill took turns on one
CPU node. Census 44 lost 3 of its first 4 goals to the wall cap; the night
before, the same backfill helped fill the docker disk.

FOUR RULES, AND EACH ONE EXISTS BECAUSE THE OBVIOUS VERSION FAILS

1. **Sustained quiet, not a snapshot.** A census leaves a 30-60 SECOND gap
   between goals with no loop running and no GPU lease held. A point-in-time
   "is anything running" check fires straight into that gap. `MIN_QUIET_SECONDS`
   must exceed the longest such gap, so a census never looks quiet mid-run.

2. **Yield mid-job.** Quiet at the start does not mean quiet throughout. A
   backfill that begins during a lull and runs for an hour is the original
   bug wearing a delay. Jobs are handed a `should_continue()` they must poll,
   and they stop where they are — the ingest checkpoints per file, so stopping
   costs nothing.

3. **One at a time.** Two background jobs that each politely wait for the box
   to be free will then both run, and contend with each other instead. The
   queue serialises them.

4. **Fail open.** An unreadable signal means "not busy". A background job that
   refuses to run because it cannot see the gate would never run again after a
   transient blip, and silently-never-running is the worse failure — the disk
   fills either way.

The queue is a pure state machine: no asyncio, no I/O, no clock of its own.
The caller supplies `now` and the busy reading, which is what makes all of the
above testable.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

#: Retry gap after a deferral. Long enough not to re-poll a 3-hour census 60
#: times; short enough to catch a real lull.
RETRY_SECONDS = 300

#: Continuous quiet required before deferrable work starts. Must exceed the
#: longest gap inside a busy period — a census's inter-goal gap is 30-60s.
MIN_QUIET_SECONDS = 600

#: If two observations are further apart than this, the gap between them was
#: NOT WITNESSED and must not be counted as quiet. Without this the queue
#: credits itself for time it never looked at: miss a few ticks while a goal
#: runs, observe "free" once afterwards, and the arithmetic says the box has
#: been quiet the whole time. Only quiet we actually saw counts.
STALE_OBSERVATION_S = 420

#: Priority bands. Lower runs first when several are due at once.
P_INTERACTIVE_SUPPORT = 10      # narration, small enrichment
P_NORMAL = 50                   # dreams, source gathering
P_BULK = 90                     # bulk embedding / backfills — always last


def _held(gate: Optional[Dict[str, Any]]) -> Optional[str]:
    """Who holds a GATED node's lease, or None. Ungated nodes have no capacity
    to contend for, so a busy one is not a reason to defer."""
    for node in ((gate or {}).get("nodes") or []):
        if not isinstance(node, dict):
            continue
        try:
            n = int(node.get("held") or 0)
        except (TypeError, ValueError):
            n = 0
        if node.get("gated") and n > 0:
            return str(node.get("owners") or "?")
    return None


def defer_reason(gate: Optional[Dict[str, Any]] = None,
                 running_loops: Any = 0,
                 dream_active: Any = False,
                 census_active: Any = False) -> str:
    """Is anything a person is waiting on in flight? "" when the box is free."""
    owner = _held(gate)
    if owner:
        return "the GPU gate is held by %s" % owner
    try:
        if int(running_loops or 0) > 0:
            return "an agent loop is running"
    except (TypeError, ValueError):
        pass                                     # unreadable -> not busy
    if census_active:
        return "a census is running"
    if dream_active:
        return "a dream cycle is running"
    return ""


def quiet_gate(reason: str, last_busy_epoch: Any, now_epoch: Any,
               min_quiet_s: Any = MIN_QUIET_SECONDS) -> str:
    """Busy now, or not quiet for long enough. "" only when both are clear."""
    if reason:
        return reason
    try:
        quiet_for = float(now_epoch) - float(last_busy_epoch)
        need = float(min_quiet_s)
    except (TypeError, ValueError):
        return ""                                # unreadable clock -> allow
    if quiet_for < need:
        return ("only %ds of quiet so far, need %ds (a census leaves 30-60s "
                "gaps between goals)" % (int(max(0, quiet_for)), int(need)))
    return ""


def describe_defer(job: str, reason: str, retry_s: Any = RETRY_SECONDS) -> str:
    """Names the job AND the blocker — "deferred" alone tells whoever reads the
    log at 03:00 nothing they can act on."""
    return ("%s deferred: %s — retrying in %ds"
            % (job or "background work", reason or "busy", int(retry_s)))


class BackgroundQueue:
    """Which deferrable job may run, and when. Pure: the caller owns the clock.

    Usage per tick:  observe(now, reason) -> pick(now) -> started/finished.
    While a job runs it polls `may_continue(reason)` and stops if that is False.
    """

    def __init__(self, min_quiet_s: Any = MIN_QUIET_SECONDS,
                 retry_s: Any = RETRY_SECONDS,
                 stale_s: Any = STALE_OBSERVATION_S):
        self.min_quiet_s = float(min_quiet_s)
        self.retry_s = float(retry_s)
        self.stale_s = float(stale_s)
        self.jobs: Dict[str, Dict[str, Any]] = {}
        self.last_busy: float = 0.0
        self.last_seen: Optional[float] = None
        self.last_reason: str = ""
        self.running: Optional[str] = None

    # ── registration ────────────────────────────────────────────────────────
    def register(self, name: str, interval_s: Any,
                 priority: int = P_NORMAL) -> None:
        self.jobs[str(name)] = {
            "name": str(name), "interval_s": float(interval_s),
            "priority": int(priority), "last_run": 0.0, "last_ok": None,
            "last_defer": "", "runs": 0, "defers": 0,
        }

    # ── the clock of busyness ───────────────────────────────────────────────
    def observe(self, now: Any, reason: str) -> None:
        """Record the current busy reading.

        Two things restart the quiet clock, and both are necessary:

        * a BUSY reading — which is what stops a census accumulating quiet
          across its own 30-60s inter-goal gaps;
        * a GAP in observation longer than `stale_s` — because unwitnessed time
          is not evidence of quiet. Miss a few ticks while a goal runs, observe
          "free" once afterwards, and naive arithmetic would credit the whole
          unobserved period as idle.
        """
        self.last_reason = reason or ""
        try:
            t = float(now)
        except (TypeError, ValueError):
            return
        if self.last_seen is not None and (t - self.last_seen) > self.stale_s:
            self.last_busy = t          # unwitnessed gap — cannot vouch for it
        if reason:
            self.last_busy = t
        self.last_seen = t

    def quiet_for(self, now: Any) -> float:
        try:
            return max(0.0, float(now) - self.last_busy)
        except (TypeError, ValueError):
            return 0.0

    # ── choosing ────────────────────────────────────────────────────────────
    def due(self, now: Any) -> List[str]:
        try:
            t = float(now)
        except (TypeError, ValueError):
            return []
        out = [j for j in self.jobs.values()
               if t - float(j["last_run"]) >= j["interval_s"]]
        out.sort(key=lambda j: (j["priority"], j["last_run"]))
        return [j["name"] for j in out]

    def pick(self, now: Any) -> Tuple[Optional[str], str]:
        """The one job that may start, or (None, why not).

        Serialised on purpose: two jobs that each wait politely for a free box
        would then both start and contend with each other.
        """
        if self.running:
            return None, "%s is already running" % self.running
        blocked = quiet_gate(self.last_reason, self.last_busy, now,
                             self.min_quiet_s)
        if blocked:
            return None, blocked
        names = self.due(now)
        if not names:
            return None, "nothing due"
        return names[0], ""

    def may_continue(self, reason: str) -> bool:
        """Polled BY a running job. False the moment the box gets busy — quiet
        at the start does not mean quiet throughout."""
        return not (reason or "")

    # ── outcomes ────────────────────────────────────────────────────────────
    def started(self, name: str, now: Any) -> None:
        if name in self.jobs:
            self.running = name
            try:
                self.jobs[name]["last_run"] = float(now)
            except (TypeError, ValueError):
                pass

    def finished(self, name: str, now: Any, ok: bool = True,
                 note: str = "") -> None:
        if name in self.jobs:
            j = self.jobs[name]
            j["last_ok"] = bool(ok)
            j["runs"] += 1
            if note:
                j["last_defer"] = note
        if self.running == name:
            self.running = None

    def deferred(self, name: str, reason: str) -> None:
        if name in self.jobs:
            self.jobs[name]["last_defer"] = reason or ""
            self.jobs[name]["defers"] += 1

    # ── observability ───────────────────────────────────────────────────────
    def status(self, now: Any = 0) -> Dict[str, Any]:
        return {
            "running": self.running,
            "busy_reason": self.last_reason,
            "quiet_for_s": int(self.quiet_for(now)),
            "min_quiet_s": int(self.min_quiet_s),
            "jobs": [
                {"name": j["name"], "priority": j["priority"],
                 "interval_s": int(j["interval_s"]), "runs": j["runs"],
                 "defers": j["defers"], "last_ok": j["last_ok"],
                 "last_defer": j["last_defer"]}
                for j in sorted(self.jobs.values(),
                                key=lambda x: (x["priority"], x["name"]))
            ],
        }
