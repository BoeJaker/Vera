"""The awaiting-idle queue: jobs that wait for a quiet box, and are pre-empted.

Producers register work here instead of running it themselves — dream cycles,
narration, session-ingestion embedding, source embedding. The queue holds the
jobs until Vera is genuinely idle, runs ONE at a time, and stops a running job
the moment anything interactive starts.

WHY IT EXISTS
2026-09-08: a transcript backfill ran straight through two censuses, because
`_scheduled_ingest_all` fired every 300s regardless of load. Measured on prod:

    embed, box otherwise idle      ~4.0s   per record
    embed, census running         ~11.1s   per record   (2.8x)

Two embeds per ingested turn, so a census goal and the backfill took turns on
one CPU node. Census 44 lost 3 of its first 4 goals to the wall cap; the night
before, the same work helped fill the docker disk.

FOUR PROPERTIES, and the first two are what a plain "check before you start"
does not give you.

1. **Pre-emption, not politeness.** A job checks `should_stop()` between ITEMS,
   not between files, and the runner cancels its in-flight task. Worst case one
   embed (~4s) rather than a whole file or a whole pass. Deferring at the start
   only was the previous fix, and it left an hour-long backfill running through
   whatever began after it.

2. **Queued, durable and inspectable.** Pending work is a list you can look at:
   what is waiting, since when, why it has not run. A decision function that
   returns "not now" leaves nothing to see, which is why the last outage was
   invisible until the wall-caps showed up.

3. **Sustained, witnessed quiet.** A census leaves 30-60s gaps between goals; a
   snapshot check fires straight into one. And quiet that was never observed is
   not quiet — miss ticks and naive arithmetic credits the gap as idle. Both
   rules live in `background_work`.

4. **One at a time, bulk last.** Two jobs that each wait politely for a free
   box would then both run and contend with each other.

Pure: the caller owns the clock, the busy reading, and the storage. That is
what makes pre-emption testable without a live Ollama node.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

#: Job states. `waiting` is the interesting one — it is what the UI shows.
WAITING, RUNNING, DONE, FAILED, PREEMPTED = (
    "waiting", "running", "done", "failed", "preempted")

#: Producers. Named so the panel can group them and so a misfiling producer is
#: obvious rather than silently lumped in with the rest.
KIND_EMBED_SESSIONS = "embed.sessions"
KIND_EMBED_SOURCES = "embed.sources"
KIND_DREAM = "dream"
KIND_NARRATOR = "narrator"

#: Bulk embedding yields to everything else deferrable; a dream or narration is
#: shorter and more visible, so it goes first when several are waiting.
KIND_PRIORITY = {
    KIND_NARRATOR: 10,
    KIND_DREAM: 20,
    KIND_EMBED_SOURCES: 80,
    KIND_EMBED_SESSIONS: 90,
}
DEFAULT_PRIORITY = 50

#: Which kinds may be STOPPED mid-flight, and which may only be GATED at start.
#:
#: Not a policy preference - a correctness constraint. A dream or a narration
#: runs an agent loop and holds the GPU gate, so every signal that says "the box
#: is in use" is true BECAUSE OF the queue's own job. Pre-empting on that reads
#: our own work as somebody else's and cancels it milliseconds after starting:
#: a livelock that presents as "the queue does nothing".
#:
#: Embedding is different and is the case that actually motivated all this: it
#: runs on the CPU nodes, starts no loop, takes no GPU gate slot, and yields
#: between records. Nothing it does can be mistaken for foreign activity, so it
#: can be stopped the instant Vera is used again.
PREEMPTIBLE_KINDS = (KIND_EMBED_SESSIONS, KIND_EMBED_SOURCES)


def is_preemptible(kind: Any) -> bool:
    return str(kind) in PREEMPTIBLE_KINDS


#: A job pre-empted this many times running is probably too big to finish in
#: the gaps. Surfaced rather than hidden: the answer is to split it, and nobody
#: can decide that if the queue silently retries forever.
PREEMPT_WARN_AFTER = 5


def make_job(job_id: str, kind: str, title: str = "", payload: Any = None,
             enqueued_at: Any = 0) -> Dict[str, Any]:
    """One queued unit of background work."""
    return {
        "id": str(job_id), "kind": str(kind),
        "title": str(title or kind),
        "payload": payload,
        "state": WAITING,
        "enqueued_at": float(enqueued_at or 0),
        "started_at": 0.0, "finished_at": 0.0,
        "priority": KIND_PRIORITY.get(str(kind), DEFAULT_PRIORITY),
        "attempts": 0, "preempts": 0,
        "progress": {"done": 0, "total": 0},
        "last_note": "",
    }


def pending(jobs: Optional[Iterable[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """Everything still waiting, in the order it will run."""
    out = [j for j in (jobs or [])
           if isinstance(j, dict) and j.get("state") == WAITING]
    out.sort(key=lambda j: (j.get("priority", DEFAULT_PRIORITY),
                            j.get("enqueued_at", 0)))
    return out


def running(jobs: Optional[Iterable[Dict[str, Any]]]) -> Optional[Dict[str, Any]]:
    for j in (jobs or []):
        if isinstance(j, dict) and j.get("state") == RUNNING:
            return j
    return None


def next_job(jobs: Optional[Iterable[Dict[str, Any]]],
             blocked_reason: str) -> Optional[Dict[str, Any]]:
    """The one job that may start now, or None.

    `blocked_reason` comes from background_work.quiet_gate — busy now, or not
    quiet for long enough. Either way nothing starts: this queue must not fire
    AT ALL during active use.
    """
    if blocked_reason:
        return None
    if running(jobs):
        return None                     # serialised on purpose
    q = pending(jobs)
    return q[0] if q else None


def preempt(job: Optional[Dict[str, Any]], reason: str,
            now: Any = 0) -> Dict[str, Any]:
    """Stop a running job and put it BACK at the front of the queue.

    Re-queued rather than failed: it was interrupted, not broken, and its
    producer checkpoints progress (the session ingest persists per file), so
    the next run resumes rather than restarts.
    """
    j = dict(job or {})
    j["state"] = WAITING
    j["preempts"] = int(j.get("preempts", 0)) + 1
    j["last_note"] = "pre-empted: %s" % (reason or "activity")
    try:
        j["finished_at"] = float(now)
    except (TypeError, ValueError):
        pass
    return j


def struggling(jobs: Optional[Iterable[Dict[str, Any]]],
               threshold: int = PREEMPT_WARN_AFTER) -> List[Dict[str, Any]]:
    """Jobs pre-empted so often they may never finish. Worth saying out loud —
    the fix is to split the job, and that is a human decision."""
    return [j for j in (jobs or [])
            if isinstance(j, dict) and int(j.get("preempts", 0)) >= threshold]


def summary(jobs: Optional[Iterable[Dict[str, Any]]],
            blocked_reason: str = "", now: Any = 0) -> Dict[str, Any]:
    """What the Ollama panel renders: depth, what is running, why it is waiting."""
    all_jobs = [j for j in (jobs or []) if isinstance(j, dict)]
    q = pending(all_jobs)
    run = running(all_jobs)
    by_kind: Dict[str, int] = {}
    oldest = 0.0
    for j in q:
        by_kind[j.get("kind", "?")] = by_kind.get(j.get("kind", "?"), 0) + 1
        try:
            age = float(now) - float(j.get("enqueued_at", 0))
            oldest = max(oldest, age)
        except (TypeError, ValueError):
            pass
    if run:
        note = "running %s" % run.get("title", run.get("id", "?"))
    elif not q:
        note = "nothing queued"
    elif blocked_reason:
        note = "%d job(s) waiting — %s" % (len(q), blocked_reason)
    else:
        note = "%d job(s) waiting — starting shortly" % len(q)
    def _row(j: Dict[str, Any]) -> Dict[str, Any]:
        """What the Ollama panel shows per job. Deliberately not the whole job:
        payloads can be large and are nobody's business in a status view."""
        try:
            waited = max(0.0, float(now) - float(j.get("enqueued_at", 0)))
        except (TypeError, ValueError):
            waited = 0.0
        return {
            "id": j.get("id", ""), "kind": j.get("kind", ""),
            "title": j.get("title", ""), "state": j.get("state", ""),
            "waiting_for_s": int(waited),
            "preempts": int(j.get("preempts", 0)),
            "attempts": int(j.get("attempts", 0)),
            "last_note": j.get("last_note", ""),
        }

    return {
        "depth": len(q),
        "running": _row(run) if run else None,
        "waiting": [_row(j) for j in q],
        "by_kind": by_kind,
        "blocked_reason": blocked_reason,
        "oldest_wait_s": int(oldest),
        "struggling": [j.get("id") for j in struggling(all_jobs)],
        "note": note,
    }
