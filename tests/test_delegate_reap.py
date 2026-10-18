"""Roadmap J5c (compute-roles, 11 Oct 2026): delegated jobs a prod restart left 'running' are settled once
at start - interrupted, worktree removed, run hash closed - but never a job another live process runs."""
import pathlib
import sys
from datetime import datetime, timezone

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vera.evolve import delegate_reap_core as C  # noqa: E402

SRC = (ROOT / "vera" / "evolve" / "delegate_capabilities.py").read_text(encoding="utf-8")
pytestmark = pytest.mark.critical
NOW = datetime(2026, 10, 11, 23, 0, tzinfo=timezone.utc).timestamp()


def test_only_active_jobs_without_a_task_and_a_silent_loop():
    job = {"status": "running", "updated_at": "2026-10-11T20:00:00Z"}
    assert C.should_reap(job, running_here=False, last_event_iso="2026-10-11T22:40:00Z", now=NOW)[0]
    assert not C.should_reap(job, running_here=True, last_event_iso=None, now=NOW)[0]
    ok, why = C.should_reap(job, running_here=False, last_event_iso="2026-10-11T22:58:00Z", now=NOW)
    assert not ok and "another process" in why
    assert not C.should_reap({"status": "done"}, running_here=False, last_event_iso=None, now=NOW)[0]
    assert not C.should_reap({"status": "interrupted"}, running_here=False, last_event_iso=None, now=NOW)[0]
    # no events: falls back to the job's own timestamps
    assert C.should_reap({"status": "starting", "created_at": "2026-10-11T21:00:00"},
                         running_here=False, last_event_iso=None, now=NOW)[0]


def test_wired_once_at_start_host_only_never_in_a_sandbox_never_prune():
    assert '_orch.start_at_import(_reap_startup, "delegate_reap")' in SRC
    body = SRC[SRC.index("async def _reap_interrupted"):SRC.index("async def _reap_startup")]
    assert "_orch.is_dev_sandbox()" in body and "_drop_worktree(job.get(\"worktree\", \"\"))" in body
    assert '"status": "interrupted"' in body and "_keep_trajectory(job)" in body
    assert "prune" not in body.replace("never prune", "")
