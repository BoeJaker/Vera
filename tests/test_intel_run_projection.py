import pytest

from vera.execution.run_journal import MemoryRunJournal
from vera.execution.run_projection import ShadowRunRegistry
from vera.intel.intel_run_projection import (
    finish_intel_job_run,
    start_intel_job_run,
)


pytestmark = pytest.mark.critical


def _registry():
    return ShadowRunRegistry(max_runs=20, journal=MemoryRunJournal())


@pytest.mark.parametrize(("result", "status", "reason"), [
    ({"ok": True}, "completed", "native_completed"),
    ({"ok": True, "skipped": "session ended"}, "skipped", "native_skipped"),
    ({"yielded": "pre-empted: person active"}, "cancelled", "native_preempted"),
    ({"ok": False, "error": "private detail"}, "failed", "native_result_error"),
])
def test_intel_native_result_projects_content_free_terminal_state(
        result, status, reason):
    registry = _registry()
    projection = start_intel_job_run(
        job_id="intel:job-1", mode="topic", attempt=2, registry=registry)
    finish_intel_job_run(projection, result=result)

    observed = registry.get(projection.run.id)
    assert observed["run"]["status"] == status
    assert observed["run"]["workflow_id"] == "intel.idle.topic"
    assert observed["run"]["attempt"] == 2
    assert observed["run"]["events"][-1]["payload"] == {
        "mode": "topic", "reason_code": reason,
    }
    assert "private detail" not in str(observed)


def test_intel_projection_failure_never_affects_native_lifecycle():
    class BrokenRegistry:
        def record(self, run, event):
            raise RuntimeError("journal unavailable")

    projection = start_intel_job_run(
        job_id="intel:job-2", mode="brief", registry=BrokenRegistry())
    finish_intel_job_run(projection, result={"ok": True})


def test_intel_exception_is_classified_without_copying_exception_text():
    registry = _registry()
    projection = start_intel_job_run(
        job_id="intel:job-3", mode="files", registry=registry)
    finish_intel_job_run(projection, error_type="RuntimeError")

    observed = registry.get(projection.run.id)
    assert observed["run"]["status"] == "failed"
    assert observed["run"]["error"] == {
        "code": "RuntimeError",
        "message": "native Intel job reported failure",
        "retryable": False,
        "details": {},
    }


def test_unknown_mode_cannot_leak_into_run_identity():
    registry = _registry()
    projection = start_intel_job_run(
        job_id="intel:job-4", mode="private search words", registry=registry)
    finish_intel_job_run(projection, result={"ok": False})

    observed = registry.get(projection.run.id)
    assert observed["run"]["kind"] == "vera.intel.unknown"
    assert observed["run"]["workflow_id"] == "intel.idle.unknown"
    assert "private search words" not in str(observed)
