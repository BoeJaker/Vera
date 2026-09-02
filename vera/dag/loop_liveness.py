"""A loop that is busy is not a loop that is dead.

Liveness is read from an activity marker in Redis (`vera:loop:sessions`), and
that marker is only written when the loop EMITS AN EVENT - the zadd lives in the
event-persist path. So the signal is "something interesting happened recently",
and it is being used to answer "is this process still working". Those differ
exactly when a loop is inside one long call, which is the moment it matters.

CENSUS 23, build-browser-verified:

    t+ 177s   operator.run  called
    t+1381s   reported "interrupted" - the census recorded the goal and moved on
    t+1522s   operator.run  returned (1,344,677 ms)
    t+7522s   agent_loop_v6.done - the loop had been working the whole time

The threshold is _LOOP_STALE_SECS = max(600, OLLAMA_GEN_TIMEOUT + 300) = 1200s,
and its own comment says it "MUST outlast one legitimate generation". It does.
But operator.run is not one generation - it is a composite call that makes up to
max_steps generations internally, so it can legitimately outlast any value
derived from a single one. The rule was right for the case it was written for
and was never revisited when a TOOL could contain a loop of its own.

Raising the threshold again would only move the boundary. The signal is wrong:
absence of events is not absence of work.

SO THE MARKER IS HEARTBEATED FOR AS LONG AS THE RUNNER TASK IS ALIVE, on a timer,
independent of whether anything was emitted. That is the question being asked, so
it is the question now answered.

WHAT THIS DELIBERATELY DOES NOT DO: it does not keep a dead run looking alive.
The heartbeat is tied to the runner task and stops when it does, so after a
server restart - the case the stale check exists for - there is no heartbeat, the
marker ages out, and the run is correctly reported interrupted. It is also
consistent with the check's own definitive path, which already treats a live task
in this process as proof of life; this extends that proof to processes that
cannot see the in-memory registry.

Pure: intervals and comparisons only. The task that uses them lives in the loop.
"""

from __future__ import annotations

from typing import Optional

#: Fraction of the stale window between heartbeats. Three beats inside the
#: window means two can be lost - to a busy event loop, a Redis blip, a slow
#: write - before a live run looks dead.
BEATS_PER_WINDOW = 4

#: Never beat more often than this: the write is cheap but not free, and a
#: sub-second heartbeat on every concurrent loop is noise, not safety.
MIN_INTERVAL_S = 15.0

#: Never go longer than this regardless of the window, so a very large
#: OLLAMA_GEN_TIMEOUT cannot produce a heartbeat too coarse to be useful.
MAX_INTERVAL_S = 120.0


def stale_secs(gen_timeout_s: float, floor_s: float = 600.0,
               margin_s: float = 300.0) -> float:
    """The window `_loop_run_is_stale` uses. Mirrored here so the interval can
    be derived from the same number rather than guessed alongside it."""
    try:
        g = float(gen_timeout_s)
    except (TypeError, ValueError):
        g = 900.0
    return max(float(floor_s), g + float(margin_s))


def heartbeat_interval(stale_window_s: float) -> float:
    """How often to refresh the marker, given the window it must stay inside."""
    try:
        w = float(stale_window_s)
    except (TypeError, ValueError):
        w = 1200.0
    if w <= 0:
        return MIN_INTERVAL_S
    return max(MIN_INTERVAL_S, min(MAX_INTERVAL_S, w / BEATS_PER_WINDOW))


def is_stale(marker_age_s: Optional[float], stale_window_s: float) -> bool:
    """Whether an activity marker of this age means the run is gone.

    A missing marker (None) is NOT staleness - it is absence of information, and
    the caller has a run record in hand saying otherwise. Deciding "dead" from
    "I have no marker" is what turned a working loop into a recorded failure.
    """
    if marker_age_s is None:
        return False
    try:
        return float(marker_age_s) > float(stale_window_s)
    except (TypeError, ValueError):
        return False


def survives_missed_beats(stale_window_s: float, missed: int = 2) -> bool:
    """Whether `missed` consecutive lost heartbeats still leave a run alive.

    The property that makes the heartbeat worth having: one dropped write must
    not resurrect the bug it fixes.
    """
    iv = heartbeat_interval(stale_window_s)
    return iv * (missed + 1) <= float(stale_window_s)
