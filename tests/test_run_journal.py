from dataclasses import replace

import pytest

from vera.execution.run_journal import (
    JournalCorruption,
    MemoryRunJournal,
    RunControlLedger,
    SqliteRunJournal,
)
from vera.execution.run_protocol import Run, RunEvent, RunStatus


pytestmark = pytest.mark.critical


def _completed_run(run_id="run-1"):
    run = Run(id=run_id, kind="vera.dag", trace_id="trace-1")
    run.transition(RunStatus.RUNNING, event_type="run.started", occurred_at="t1")
    run.transition(RunStatus.COMPLETED, event_type="run.completed", occurred_at="t2")
    return run


def test_journal_appends_gap_free_events_and_rebuilds_same_projection():
    original = _completed_run()
    journal = MemoryRunJournal()
    rows = [journal.append(event) for event in original.events]

    assert rows[0].previous_checksum == ""
    assert rows[1].previous_checksum == rows[0].checksum
    rebuilt = journal.rebuild(run_id=original.id, kind=original.kind,
                              trace_id=original.trace_id)
    assert rebuilt.status == RunStatus.COMPLETED
    assert rebuilt.started_at == "t1" and rebuilt.ended_at == "t2"
    assert [event.id for event in rebuilt.events] == [event.id for event in original.events]


def test_append_is_idempotent_for_same_event_and_rejects_conflicting_id():
    event = _completed_run().events[0]
    journal = MemoryRunJournal()
    first = journal.append(event)
    assert journal.append(event) is first

    conflicting = replace(event, run_id="another-run")
    with pytest.raises(JournalCorruption, match="reused"):
        journal.append(conflicting)


def test_journal_rejects_sequence_gaps_before_writing():
    event = replace(_completed_run().events[0], sequence=2)
    journal = MemoryRunJournal()
    with pytest.raises(JournalCorruption, match="gap-free"):
        journal.append(event)
    assert journal.entries(event.run_id) == []


@pytest.mark.parametrize("damage", ["previous", "checksum", "identity"])
def test_journal_detects_corruption_before_rebuild_export_or_delete(damage):
    run = _completed_run()
    journal = MemoryRunJournal()
    for event in run.events:
        journal.append(event)
    row = journal._entries[run.id][1]
    if damage == "previous":
        broken = replace(row, previous_checksum="wrong")
    elif damage == "checksum":
        broken = replace(row, checksum="wrong")
    else:
        broken = replace(row, event=replace(row.event, run_id="other"))
    journal._entries[run.id][1] = broken

    with pytest.raises(JournalCorruption):
        journal.verify(run.id)
    with pytest.raises(JournalCorruption):
        journal.rebuild(run_id=run.id, kind=run.kind)
    with pytest.raises(JournalCorruption):
        journal.export(run.id)
    with pytest.raises(JournalCorruption):
        journal.delete(run.id, expected_checksum="wrong")


def test_export_is_portable_and_delete_requires_verified_tip():
    run = _completed_run()
    journal = MemoryRunJournal()
    for event in run.events:
        journal.append(event)
    exported = journal.export(run.id)

    assert exported["protocol"] == "vera.run.v1"
    assert exported["event_count"] == 2
    assert len(exported["last_checksum"]) == 64
    assert len(exported["entries"]) == 2

    with pytest.raises(ValueError, match="does not match"):
        journal.delete(run.id, expected_checksum="wrong")
    assert len(journal.entries(run.id)) == 2
    assert journal.delete(run.id, expected_checksum=exported["last_checksum"]) is True
    assert journal.entries(run.id) == []
    assert journal.delete(run.id, expected_checksum=exported["last_checksum"]) is False


def test_sqlite_journal_survives_reopen_and_rebuilds_projection(tmp_path):
    path = tmp_path / "runs.sqlite3"
    run = _completed_run()
    first = SqliteRunJournal(path)
    for event in run.events:
        first.append(event)
    checksum = first.export(run.id)["last_checksum"]
    first.close()

    reopened = SqliteRunJournal(path)
    assert reopened.run_ids() == [run.id]
    assert reopened.verify(run.id) == {
        "ok": True, "run_id": run.id, "event_count": 2,
        "last_checksum": checksum}
    rebuilt = reopened.rebuild(run_id=run.id, kind=run.kind,
                               trace_id=run.trace_id)
    assert rebuilt.status == RunStatus.COMPLETED
    assert [event.id for event in rebuilt.events] == [event.id for event in run.events]
    reopened.close()


def test_sqlite_journal_preserves_idempotency_and_guarded_delete(tmp_path):
    journal = SqliteRunJournal(tmp_path / "runs.sqlite3")
    run = _completed_run()
    first = journal.append(run.events[0])
    assert journal.append(run.events[0]) == first
    with pytest.raises(JournalCorruption, match="reused"):
        journal.append(replace(run.events[0], run_id="other"))
    journal.append(run.events[1])
    with pytest.raises(ValueError, match="does not match"):
        journal.delete(run.id, expected_checksum="wrong")
    checksum = journal.export(run.id)["last_checksum"]
    assert journal.delete(run.id, expected_checksum=checksum) is True
    journal.close()


def test_controls_record_intent_and_native_acknowledgement_without_execution():
    ledger = RunControlLedger()
    requested = ledger.request(run_id="run-1", action="retry",
                               requested_by="operator", reason="transient")
    assert requested.status == "requested"
    assert ledger.get(requested.id) == requested

    acknowledged = ledger.decide(requested.id, accepted=True,
                                 acknowledged_by="vera.dag")
    assert acknowledged.status == "acknowledged"
    assert acknowledged.acknowledged_by == "vera.dag"
    assert ledger.export("run-1")[0]["action"] == "retry"
    with pytest.raises(ValueError, match="already decided"):
        ledger.decide(requested.id, accepted=False, acknowledged_by="other")


def test_controls_can_be_rejected_and_unknown_actions_fail_closed():
    ledger = RunControlLedger()
    requested = ledger.request(run_id="run-1", action="cancel")
    rejected = ledger.decide(requested.id, accepted=False,
                             acknowledged_by="external.engine", reason="terminal")
    assert rejected.status == "rejected"
    assert rejected.reason == "terminal"

    with pytest.raises(ValueError, match="unsupported"):
        ledger.request(run_id="run-1", action="delete_everything")
    with pytest.raises(KeyError, match="not found"):
        ledger.decide("missing", accepted=True, acknowledged_by="engine")
