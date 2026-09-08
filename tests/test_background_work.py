"""Background work must not run while the system is in use — including in the
30-second gaps *inside* a census.

2026-09-08, twice. A transcript backfill ran straight through two censuses.
Measured from prod's log:

    embed, box otherwise idle      ~4.0s   per record
    embed, census running         ~11.1s   per record   (2.8x)

Two embeds per ingested turn (`data_fabric.py:_embed` and
`memory.py:embed_text`), so a census goal and the backfill took turns on one
CPU node. Census 44 lost 3 of its first 4 goals to the wall cap.

The two tests that matter here are the ones that kill the obvious fix:

  * `test_a_gap_between_census_goals_does_not_look_quiet` — a point-in-time
    "is anything running" check passes in the 30-60s between goals, starts a
    backlog of 11-second embeds, and the next goal runs contended.
  * `test_a_job_yields_when_the_box_gets_busy_mid_run` — quiet at the start
    does not mean quiet throughout; an hour-long backfill that began in a lull
    is the same bug with a delay.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from vera import background_work as BG                      # noqa: E402


GATE_FREE = {"nodes": [{"node": "gpu-250", "gated": True, "held": 0, "owners": []},
                       {"node": "cpu-246", "gated": False, "held": None}]}
GATE_HELD = {"nodes": [{"node": "gpu-250", "gated": True, "held": 1,
                        "owners": ["LLM:3690174:6b3dce0c"]}]}


# ── what counts as busy ─────────────────────────────────────────────────────
def test_a_held_gpu_lease_is_busy_and_names_the_owner():
    r = BG.defer_reason(GATE_HELD, 0)
    assert r and "6b3dce0c" in r


def test_a_running_loop_is_busy():
    assert BG.defer_reason(GATE_FREE, 1) == "an agent loop is running"


def test_a_census_is_busy_even_with_a_free_gate_and_no_loop():
    """Between goals there is momentarily neither — the census itself has to
    count, which is why census_active is a separate signal."""
    assert BG.defer_reason(GATE_FREE, 0, census_active=True) == "a census is running"


def test_a_dream_cycle_is_busy():
    assert BG.defer_reason(GATE_FREE, 0, dream_active=True) == "a dream cycle is running"


def test_an_idle_box_is_free():
    assert BG.defer_reason(GATE_FREE, 0) == ""


def test_an_ungated_node_is_not_contention():
    busy_cpu = {"nodes": [{"node": "cpu-246", "gated": False, "held": 5}]}
    assert BG.defer_reason(busy_cpu, 0) == ""


def test_unreadable_signals_fail_OPEN():
    """A job that refuses to run because it cannot read the gate would never
    run again after a blip — and the disk fills either way."""
    for bad in (None, {}, {"nodes": None}, {"nodes": ["junk"]}):
        assert BG.defer_reason(bad, 0) == ""
    assert BG.defer_reason(GATE_FREE, None) == ""
    assert BG.defer_reason(GATE_FREE, "x") == ""


# ── THE BUG: a momentary gap must not look quiet ────────────────────────────
def test_a_gap_between_census_goals_does_not_look_quiet():
    """The whole point. A census leaves 30-60s between goals with no loop and
    no lease. A snapshot check fires straight into it."""
    q = BG.BackgroundQueue(min_quiet_s=600)
    q.register("ingest", interval_s=300, priority=BG.P_BULK)
    q.observe(1000.0, "an agent loop is running")     # goal 1 running
    q.observe(1040.0, "")                             # 40s gap between goals
    name, why = q.pick(1040.0)
    assert name is None
    assert "only 40s of quiet" in why and "need 600s" in why


def test_quiet_must_be_CONTINUOUS_not_cumulative():
    """Twelve 40-second gaps are not eight minutes of quiet. Every busy
    observation restarts the clock. This is a whole census: twelve 30-minute
    goals each followed by a 40-second gap, with the scheduler ticking every
    300s throughout — so the goal is observed busy many times."""
    q = BG.BackgroundQueue(min_quiet_s=600)
    q.register("ingest", 300, BG.P_BULK)
    t = 1000.0
    for _ in range(12):
        for _ in range(6):                      # 6 ticks x 300s = one goal
            q.observe(t, "an agent loop is running")
            assert q.pick(t)[0] is None
            t += 300
        q.observe(t, "")                        # the gap between goals
        t += 40
        assert q.pick(t)[0] is None, "fired in an inter-goal gap"
    # Across a whole census the queue never accrues its quiet window.
    assert q.quiet_for(t) < BG.MIN_QUIET_SECONDS


def test_UNWITNESSED_time_is_not_quiet():
    """The subtler hole, found by the test above before it was corrected.

    If the scheduler misses ticks — the process was busy, blocked, or the loop
    stalled — a single "free" reading afterwards must not license a run. The
    queue only counts quiet it actually observed.
    """
    q = BG.BackgroundQueue(min_quiet_s=600, stale_s=420)
    q.register("ingest", 300, BG.P_BULK)
    q.observe(1000.0, "an agent loop is running")
    # ...30 minutes pass with NO observations at all...
    q.observe(2800.0, "")
    assert q.pick(2800.0)[0] is None, "credited itself for unobserved time"
    # once it starts observing continuously again, quiet accrues normally
    q.observe(3100.0, ""); q.observe(3400.0, "")
    assert q.pick(3400.0)[0] == "ingest"


def test_after_real_quiet_it_runs():
    """Quiet has to be OBSERVED, so the census ends and the scheduler keeps
    ticking on a free box until the window is genuinely accrued."""
    q = BG.BackgroundQueue(min_quiet_s=600)
    q.register("ingest", 300, BG.P_BULK)
    q.observe(1000.0, "a census is running")
    t = 1000.0
    for _ in range(3):                      # three ticks of witnessed quiet
        t += 300
        q.observe(t, "")
    assert q.pick(t) == ("ingest", "")


# ── THE OTHER BUG: it must yield mid-run ────────────────────────────────────
def test_a_job_yields_when_the_box_gets_busy_mid_run():
    """Quiet at the start does not mean quiet throughout. A backfill that
    begins in a lull and runs for an hour is the original bug with a delay."""
    q = BG.BackgroundQueue()
    assert q.may_continue("") is True
    assert q.may_continue("a census is running") is False


# ── one at a time ───────────────────────────────────────────────────────────
def test_two_background_jobs_do_not_run_together():
    """Both would politely wait for a free box, then contend with each other."""
    q = BG.BackgroundQueue(min_quiet_s=0)
    q.register("ingest", 0, BG.P_BULK)
    q.register("dream", 0, BG.P_NORMAL)
    q.observe(100.0, "")
    name, _ = q.pick(100.0)
    q.started(name, 100.0)
    assert q.pick(101.0) == (None, "%s is already running" % name)


def test_finishing_frees_the_queue():
    q = BG.BackgroundQueue(min_quiet_s=0)
    q.register("ingest", 0, BG.P_BULK)
    q.observe(100.0, "")
    q.started("ingest", 100.0)
    q.finished("ingest", 200.0, ok=True)
    assert q.running is None


def test_bulk_work_runs_LAST():
    """A backfill must never delay narration or a dream."""
    q = BG.BackgroundQueue(min_quiet_s=0)
    q.register("ingest", 0, BG.P_BULK)
    q.register("narrate", 0, BG.P_INTERACTIVE_SUPPORT)
    q.register("dream", 0, BG.P_NORMAL)
    q.observe(100.0, "")
    assert q.pick(100.0)[0] == "narrate"


# ── scheduling ──────────────────────────────────────────────────────────────
def test_a_job_is_not_due_before_its_interval():
    q = BG.BackgroundQueue(min_quiet_s=0)
    q.register("ingest", interval_s=300, priority=BG.P_BULK)
    q.observe(1000.0, "")
    q.started("ingest", 1000.0)
    q.finished("ingest", 1000.0)
    assert q.pick(1100.0) == (None, "nothing due")       # 100s < 300s
    assert q.pick(1400.0)[0] == "ingest"


def test_status_reports_why_it_is_waiting():
    """"deferred" alone tells whoever reads the log at 03:00 nothing."""
    q = BG.BackgroundQueue(min_quiet_s=600)
    q.register("ingest", 300, BG.P_BULK)
    q.observe(1000.0, "a census is running")
    _, why = q.pick(1000.0)
    q.deferred("ingest", why)
    s = q.status(1000.0)
    assert s["busy_reason"] == "a census is running"
    assert s["jobs"][0]["defers"] == 1
    assert s["jobs"][0]["last_defer"] == "a census is running"


def test_the_log_line_names_the_job_and_the_blocker():
    m = BG.describe_defer("transcript ingest", "a census is running", 300)
    assert "transcript ingest" in m and "census" in m and "300" in m


# ── the numbers that justify the design ─────────────────────────────────────
def test_the_quiet_window_exceeds_a_census_inter_goal_gap():
    """Measured gaps are 30-60s. The window must comfortably exceed them or
    this whole mechanism leaks."""
    assert BG.MIN_QUIET_SECONDS >= 300


def test_an_unreadable_clock_does_not_wedge_the_queue():
    assert BG.quiet_gate("", None, None) == ""
    assert BG.quiet_gate("", "x", "y") == ""
