"""DEEP-21 D21-5 (compute-roles, 12 Oct 2026): background schedule runs a restart killed are closed as
interrupted at start, and every non-census kind's unfinished run holds its schedule's next start."""
import pathlib
import sys
from datetime import datetime, timedelta, timezone

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vera.evolve import schedule_core as core  # noqa: E402

SRC = (ROOT / "vera" / "evolve" / "schedule_capabilities.py").read_text(encoding="utf-8")
pytestmark = pytest.mark.critical
T = datetime(2026, 10, 12, 3, 0, tzinfo=timezone.utc)


def test_interrupted_runs_are_old_unfinished_non_census():
    runs = [{"id": "a", "kind": "cap", "started_at": core.iso(T - timedelta(hours=1)), "finished_at": ""},
            {"id": "b", "kind": "census", "started_at": core.iso(T - timedelta(hours=1)), "finished_at": ""},
            {"id": "c", "kind": "pipeline", "started_at": core.iso(T - timedelta(hours=1)), "finished_at": core.iso(T)},
            {"id": "d", "kind": "suite", "started_at": core.iso(T + timedelta(minutes=1)), "finished_at": ""}]
    assert [r["id"] for r in core.interrupted_runs(runs, T)] == ["a"]


def test_every_background_kind_waits_for_its_previous_run():
    assert set(core.RUN_TRACKED_KINDS) == set(core.KINDS) - {"census"}
    assert set(core.BACKGROUND_KINDS) == {"loop", "tests"}          # continuous cadence unchanged


def test_reconcile_runs_once_at_start():
    assert '_orch.start_at_import(_reconcile_startup, "schedule_reconcile")' in SRC
    assert 'result="interrupted"' in SRC
