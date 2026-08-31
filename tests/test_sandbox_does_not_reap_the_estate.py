"""A dev sandbox must not reap the box it is a guest on.

Leech boot exists so a dev sandbox "never fires heavy ambient jobs". The jobs
that REAP THE SHARED ESTATE - containers, worktrees, branches and the sandbox
pool registry every instance reads - were simply never on the list.

Measured during census 20 (2026-08-31): a prune ran at 14:58 UTC emitting the
PRE-fix audit text, about eighteen minutes after prod restarted WITH that fix,
so it ran somewhere else. The only Vera-image container running at that moment
was another agent's dev sandbox, idle-paused four minutes later. It emptied the
sandbox pool - and the operator, which falls back to any registered sandbox when
the primary is occupied, then had nothing to choose from and logged "the sandbox
list was empty (or could not be read)".

Two mechanisms guard this now and they are NOT redundant: singleton=True gates a
job to one instance, but that lease only binds instances running current code;
skip_in_sandbox keeps it out of dev containers regardless of which instance
happens to hold the lease.
"""
import ast
import os
import re

_ROOT = os.path.join(os.path.dirname(__file__), "..")
_ORCH = os.path.join(_ROOT, "vera", "capability_orchestration.py")
_EVOLVE = os.path.join(_ROOT, "vera", "evolve", "evolve_capabilities.py")

#: Jobs that mutate state shared by every instance on the box.
ESTATE_JOBS = {
    "evolve.sandbox.idle_sweep",
    "evolve.scaffolding.sweep",
    "evolve.mainline_mirror.refresh",
}


def _skip_set():
    with open(_ORCH, encoding="utf-8") as fh:
        src = fh.read()
    start = src.index("_SANDBOX_SKIP_JOBS = {")
    end = src.index("}", start)
    return set(re.findall(r'"([^"]+)"', src[start:end]))


def _scheduled(name):
    """The schedule(...) call registering `name`, as source."""
    with open(_EVOLVE, encoding="utf-8") as fh:
        src = fh.read()
    i = src.index('name="%s"' % name)
    start = src.rindex("schedule(", 0, i)
    return src[start:src.index(")", i) + 1]


def test_every_estate_reaping_job_is_skipped_in_a_dev_sandbox():
    missing = sorted(ESTATE_JOBS - _skip_set())
    assert not missing, (
        "these reap the shared estate and would run inside a dev sandbox: %s" % missing)


def test_every_estate_reaping_job_is_also_a_singleton():
    """The other guard. Neither replaces the other: the lease binds only
    instances running current code; the skip binds every dev container."""
    for job in sorted(ESTATE_JOBS):
        assert "singleton=True" in _scheduled(job), "%s is not a singleton" % job


def test_the_jobs_named_here_really_exist():
    """If a job is renamed, this must fail rather than protect nothing."""
    for job in sorted(ESTATE_JOBS):
        assert 'name="%s"' % job in open(_EVOLVE, encoding="utf-8").read()


def test_the_skip_list_still_holds_its_original_entries():
    """Widening it must not quietly drop what was already there."""
    skips = _skip_set()
    for original in ("agent_rag_refresh", "bench_node_perf", "longterm_scheduler",
                     "worldview_startup_load"):
        assert original in skips


def test_the_scheduler_consults_the_skip_list():
    with open(_ORCH, encoding="utf-8") as fh:
        src = fh.read()
    assert 'task["name"] in _SANDBOX_SKIP_JOBS' in src


def test_the_reason_is_recorded_next_to_the_list():
    """So the next person does not have to re-derive why these are here."""
    with open(_ORCH, encoding="utf-8") as fh:
        src = fh.read()
    i = src.index("evolve.sandbox.idle_sweep")
    window = src[max(0, i - 1400):i]
    assert "REAP THE SHARED ESTATE" in window
    assert "census 20" in window or "14:58" in window
