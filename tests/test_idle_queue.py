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


# â”€â”€ what the Ollama panel renders â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
def test_summary_carries_the_rows_the_panel_needs():
    """The panel shows a LIST, not just a count - "3 waiting" with no way to
    see what, since when, or why is the opacity this queue exists to remove."""
    now = 1000.0
    jobs = [
        Q.make_job("a", Q.KIND_EMBED_SESSIONS, "backfill", enqueued_at=now - 90),
        Q.make_job("b", Q.KIND_NARRATOR, "quick take", enqueued_at=now - 30),
    ]
    s = Q.summary(jobs, "a census is running", now)
    assert s["depth"] == 2
    kinds = [r["kind"] for r in s["waiting"]]
    assert kinds == [Q.KIND_NARRATOR, Q.KIND_EMBED_SESSIONS], \
        "waiting rows must be in the order they will run (narrator first)"
    assert s["waiting"][1]["waiting_for_s"] == 90
    assert s["blocked_reason"] == "a census is running"


def test_summary_rows_do_not_leak_payloads():
    """A status view is read by anything that can see the panel; job payloads
    are not part of that contract."""
    j = Q.make_job("a", Q.KIND_DREAM, "d", payload={"secret": "x"},
                    enqueued_at=1.0)
    row = Q.summary([j], "", 2.0)["waiting"][0]
    assert "payload" not in row


# â”€â”€ stranded records â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# The store is durable; the asyncio task is not. Observed on prod 2026-09-09:
# embed.sessions marked `running` with nothing running it, runs=0, and
# embed.sources waiting 17.6 HOURS behind it having never been offered the box
# once. next_job refuses to start anything while any job is running, so a
# single stale record is a permanent, silent deadlock.
def _running_job(jid="embed.sessions:abc"):
    return {"id": jid, "kind": Q.KIND_EMBED_SESSIONS, "title": "backfill",
            "state": Q.RUNNING, "enqueued_at": 0, "attempts": 1}


def test_a_running_record_with_no_live_runner_is_stranded():
    assert [j["id"] for j in Q.stranded([_running_job()], live_ids=[])] \
        == ["embed.sessions:abc"]


def test_a_running_record_the_caller_vouches_for_is_not_stranded():
    assert Q.stranded([_running_job()], live_ids=["embed.sessions:abc"]) == []


def test_waiting_jobs_are_never_stranded():
    waiting = dict(_running_job(), state=Q.WAITING)
    assert Q.stranded([waiting], live_ids=[]) == []


def test_stranded_survives_rubbish():
    for junk in (None, [], [None], ["nope"], [{}]):
        assert Q.stranded(junk, live_ids=[]) == []


def test_requeueing_puts_it_back_in_the_queue():
    back = Q.requeue_stranded(_running_job(), now=100)
    assert back["state"] == Q.WAITING
    assert back["started_at"] is None


def test_a_strand_is_counted_apart_from_a_preemption():
    """A rising preempt count means the box is busy; a rising strand count
    means runners are dying. Summed, they look like one problem."""
    back = Q.requeue_stranded(_running_job(), now=100)
    assert back["stranded"] == 1
    assert back.get("preempts", 0) == 0
    assert Q.requeue_stranded(back, now=200)["stranded"] == 2


def test_the_note_says_what_happened():
    assert "no live runner" in Q.requeue_stranded(_running_job(), now=1)["note"]


def test_a_requeued_job_is_selectable_again_and_unblocks_the_queue():
    """THE regression. Before: one stale record and next_job returned None for
    every job, forever."""
    other = {"id": "embed.sources:x", "kind": Q.KIND_EMBED_SOURCES,
             "state": Q.WAITING, "enqueued_at": 0}
    jobs = [_running_job(), other]
    assert Q.next_job(jobs, "") is None          # deadlocked
    fixed = [Q.requeue_stranded(j, 1) if j["state"] == Q.RUNNING else j
             for j in jobs]
    assert Q.next_job(fixed, "") is not None     # unblocked


def test_reconciling_does_not_start_anything_while_blocked():
    """Reconciliation must only move records back to waiting - the gate still
    decides whether the box is free."""
    fixed = [Q.requeue_stranded(_running_job(), 1)]
    assert Q.next_job(fixed, "a census is running") is None


def test_the_row_reports_the_strand_count():
    row = Q.summary([Q.requeue_stranded(_running_job(), 1)], "", 10)
    assert row["waiting"][0]["stranded"] == 1


# â”€â”€ progress, the field nothing read â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# make_job has always put progress {done,total} on EVERY job and nothing ever
# read it, so no producer had a reason to fill it in and no kind could be
# estimated. Reading it is what closes that gap once for every producer rather
# than one subsystem at a time.
def test_an_unset_total_means_unknown_not_nothing_left():
    """THE distinction. 'we do not know how much is left' and 'there is nothing
    left' are opposite facts, and an estimator handed 0 would confidently
    report no work to do."""
    assert Q.remaining({"progress": {"done": 0, "total": 0}}) is None
    assert Q.remaining({}) is None
    assert Q.remaining(None) is None


def test_remaining_is_total_minus_done():
    assert Q.remaining({"progress": {"done": 30, "total": 100}}) == 70


def test_a_finished_job_has_nothing_remaining_which_is_not_unknown():
    assert Q.remaining({"progress": {"done": 100, "total": 100}}) == 0


def test_overshoot_does_not_go_negative():
    assert Q.remaining({"progress": {"done": 140, "total": 100}}) == 0


def test_a_producer_that_knows_better_wins():
    j = {"items_remaining": 5, "progress": {"done": 0, "total": 100}}
    assert Q.remaining(j) == 5


def test_rubbish_progress_reads_as_unknown_rather_than_raising():
    for bad in ({"progress": "nope"}, {"progress": {"total": "x"}},
                {"progress": {"total": -3}}, {"items_remaining": "soon"}):
        assert Q.remaining(bad) is None, bad


def test_with_progress_leaves_the_caller_dict_alone():
    j = {"id": "a", "progress": {"done": 1, "total": 10}}
    Q.with_progress(j, done=5)
    assert j["progress"]["done"] == 1


def test_with_progress_only_changes_what_it_was_given():
    j = {"progress": {"done": 1, "total": 10}}
    assert Q.with_progress(j, done=4)["progress"] == {"done": 4, "total": 10}
    assert Q.with_progress(j, total=20)["progress"] == {"done": 1, "total": 20}


def test_with_progress_works_on_a_job_that_never_had_any():
    assert Q.with_progress({"id": "a"}, done=2, total=9)["progress"] \
        == {"done": 2, "total": 9}


def test_the_row_carries_progress_and_remaining_for_the_panel():
    """A job 900 of 1000 through is a different thing to schedule from one
    that has not started."""
    j = {"id": "a", "kind": Q.KIND_EMBED_SESSIONS, "state": Q.WAITING,
         "enqueued_at": 0, "progress": {"done": 900, "total": 1000}}
    row = Q.summary([j], "", 10)["waiting"][0]
    assert row["progress"] == {"done": 900, "total": 1000}
    assert row["remaining"] == 100


# â”€â”€ fabric backfill gets its own kind â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# register_handler is a plain dict assignment, so a second producer claiming
# embed.sources (which the agent knowledge sweep owns) would silently replace
# it, and queued agent-sweep jobs would then run the fabric backfill under
# their own name. Nothing would look wrong until the output did.
def test_the_fabric_backfill_has_a_kind_of_its_own():
    assert Q.KIND_EMBED_FABRIC == "embed.fabric"
    assert Q.KIND_EMBED_FABRIC != Q.KIND_EMBED_SOURCES


def test_it_is_preemptible_like_the_other_embedding_work():
    """CPU embedding, no GPU gate slot, yields between batches - nothing it
    does can be mistaken for foreign activity."""
    assert Q.is_preemptible(Q.KIND_EMBED_FABRIC)


def test_it_sits_between_the_agent_sweep_and_the_transcript_backfill():
    """The queue runs one job at a time, so ordering by expected length is what
    stops a long job holding up a short one."""
    assert Q.KIND_PRIORITY[Q.KIND_EMBED_SOURCES] \
        < Q.KIND_PRIORITY[Q.KIND_EMBED_FABRIC] \
        < Q.KIND_PRIORITY[Q.KIND_EMBED_SESSIONS]


def test_a_shorter_job_is_offered_the_box_first():
    jobs = [{"id": "f", "kind": Q.KIND_EMBED_FABRIC, "state": Q.WAITING,
             "enqueued_at": 0, "priority": Q.KIND_PRIORITY[Q.KIND_EMBED_FABRIC]},
            {"id": "a", "kind": Q.KIND_EMBED_SOURCES, "state": Q.WAITING,
             "enqueued_at": 0, "priority": Q.KIND_PRIORITY[Q.KIND_EMBED_SOURCES]}]
    assert Q.next_job(jobs, "")["id"] == "a"


def test_every_declared_kind_has_a_priority():
    """A kind missing from KIND_PRIORITY silently takes DEFAULT_PRIORITY, which
    would put a bulk embed ahead of a narration."""
    for kind in (Q.KIND_EMBED_SESSIONS, Q.KIND_EMBED_SOURCES,
                 Q.KIND_EMBED_FABRIC, Q.KIND_DREAM, Q.KIND_NARRATOR):
        assert kind in Q.KIND_PRIORITY, kind
