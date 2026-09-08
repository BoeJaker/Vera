"""The idle queue must not fire during active use, and must be pre-emptible.

2026-09-08, three times. A transcript backfill ran through two censuses because
the ingest fired every 300s regardless of load. Measured on prod: an embed
costs ~4.0s on an idle box and ~11.1s while a census runs, two per ingested
turn. Census 44 lost 3 of its first 4 goals to the wall cap.

Deferring at the START was the previous fix and it was not enough: a job that
begins in a lull and runs for an hour is the same bug with a delay. So the
tests that matter here are pre-emption and "nothing starts while busy" — not
the queue bookkeeping.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from vera import idle_queue as Q                            # noqa: E402


def jobs():
    return [
        Q.make_job("a", Q.KIND_EMBED_SESSIONS, "backfill transcripts", enqueued_at=100),
        Q.make_job("b", Q.KIND_DREAM, "nightly dream", enqueued_at=200),
        Q.make_job("c", Q.KIND_NARRATOR, "narrate the estate", enqueued_at=300),
        Q.make_job("d", Q.KIND_EMBED_SOURCES, "embed sources", enqueued_at=400),
    ]


# ── it must not fire at all during active use ───────────────────────────────
def test_nothing_starts_while_the_box_is_busy():
    """The whole requirement. Not 'starts and yields' — does not start."""
    assert Q.next_job(jobs(), "a census is running") is None
    assert Q.next_job(jobs(), "an agent loop is running") is None
    assert Q.next_job(jobs(), "the GPU gate is held by LLM:123") is None


def test_nothing_starts_in_a_momentary_gap():
    """background_work supplies the reason; a 30-60s inter-goal gap reads as
    'not quiet for long enough', and that still blocks."""
    assert Q.next_job(jobs(), "only 40s of quiet so far, need 600s") is None


def test_it_starts_when_genuinely_idle():
    j = Q.next_job(jobs(), "")
    assert j is not None and j["id"] == "c"     # narrator, highest priority


# ── ordering ────────────────────────────────────────────────────────────────
def test_bulk_embedding_runs_LAST():
    """A transcript backfill must never delay a dream or narration."""
    order = [j["id"] for j in Q.pending(jobs())]
    assert order == ["c", "b", "d", "a"]        # narrator, dream, sources, sessions


def test_same_priority_is_first_come_first_served():
    js = [Q.make_job("old", Q.KIND_DREAM, enqueued_at=100),
          Q.make_job("new", Q.KIND_DREAM, enqueued_at=200)]
    assert [j["id"] for j in Q.pending(js)] == ["old", "new"]


def test_only_one_runs_at_a_time():
    """Two jobs that each waited politely would then contend with each other."""
    js = jobs()
    js[0]["state"] = Q.RUNNING
    assert Q.next_job(js, "") is None
    assert Q.running(js)["id"] == "a"


# ── pre-emption ─────────────────────────────────────────────────────────────
def test_a_preempted_job_goes_BACK_on_the_queue():
    """It was interrupted, not broken. Its producer checkpoints progress, so
    the next run resumes rather than restarts."""
    j = Q.make_job("a", Q.KIND_EMBED_SESSIONS, enqueued_at=100)
    j["state"] = Q.RUNNING
    out = Q.preempt(j, "an agent loop is running", now=500)
    assert out["state"] == Q.WAITING
    assert out["preempts"] == 1
    assert "an agent loop is running" in out["last_note"]


def test_a_preempted_job_is_still_pending_and_keeps_its_place():
    j = Q.preempt(Q.make_job("a", Q.KIND_DREAM, enqueued_at=100), "busy")
    assert [x["id"] for x in Q.pending([j])] == ["a"]


def test_repeated_preemption_is_surfaced_not_hidden():
    """A job pre-empted forever never finishes. The fix is to split it, and
    nobody can decide that if the queue retries silently."""
    j = Q.make_job("a", Q.KIND_EMBED_SESSIONS)
    for _ in range(Q.PREEMPT_WARN_AFTER):
        j = Q.preempt(j, "busy")
    assert Q.struggling([j]) and Q.struggling([j])[0]["id"] == "a"
    assert "a" in Q.summary([j])["struggling"]


def test_a_job_preempted_once_is_not_flagged():
    j = Q.preempt(Q.make_job("a", Q.KIND_DREAM), "busy")
    assert Q.struggling([j]) == []


# ── what the panel shows ────────────────────────────────────────────────────
def test_the_summary_says_depth_and_WHY_it_is_waiting():
    s = Q.summary(jobs(), "a census is running", now=1000)
    assert s["depth"] == 4
    assert "census" in s["note"] and "4 job(s) waiting" in s["note"]


def test_the_summary_groups_by_producer():
    s = Q.summary(jobs(), "", now=1000)
    assert s["by_kind"][Q.KIND_EMBED_SESSIONS] == 1
    assert s["by_kind"][Q.KIND_DREAM] == 1


def test_the_summary_reports_the_longest_wait():
    """A job queued three hours ago is the signal that the box is never idle."""
    s = Q.summary(jobs(), "a census is running", now=4000)
    assert s["oldest_wait_s"] == 3900       # the 100-stamped one


def test_a_running_job_is_named():
    js = jobs()
    js[0]["state"] = Q.RUNNING
    s = Q.summary(js, "", now=1000)
    assert s["running"]["id"] == "a" and "backfill transcripts" in s["note"]


def test_an_empty_queue_says_so():
    assert Q.summary([], "")["note"] == "nothing queued"
    assert Q.summary(None, "")["depth"] == 0


# ── robustness ──────────────────────────────────────────────────────────────
def test_junk_entries_are_ignored():
    assert Q.pending([None, "x", 3]) == []
    assert Q.running([None, "x"]) is None
    assert Q.summary([None, 7], "")["depth"] == 0


def test_an_unknown_producer_still_queues():
    """A new producer must not be dropped just because it has no priority."""
    j = Q.make_job("z", "something.new")
    assert j["priority"] == Q.DEFAULT_PRIORITY
    assert Q.pending([j])[0]["id"] == "z"


def test_a_bad_timestamp_does_not_break_the_summary():
    j = Q.make_job("a", Q.KIND_DREAM)
    j["enqueued_at"] = "nonsense"
    assert Q.summary([j], "", now=1000)["depth"] == 1
