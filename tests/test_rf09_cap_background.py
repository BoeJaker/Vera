"""RF-09 (recon-fixes, H; compute-roles 11 Oct 2026): Loop Lab `cap` schedules ran inline in the scheduler
tick under _TICK_LOCK - a long cap blocked all scheduling, could not be gated, and a census could start
beside it. Now: background runs; no census while an exclusive cap runs; exclusive caps wait for loops."""
import pathlib
import sys
from datetime import datetime, timezone

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vera.evolve import schedule_core as core  # noqa: E402

SRC = (ROOT / "vera" / "evolve" / "schedule_capabilities.py").read_text(encoding="utf-8")
pytestmark = pytest.mark.critical
NOW = datetime(2026, 10, 12, 2, 0, tzinfo=timezone.utc)


def _rec(kind, **kw):
    base = {"id": "s1", "kind": kind, "title": "t", "days": list(range(7)), "start": "00:00",
            "end": "23:59", "timezone": "UTC", "repeat": "continuous", "enabled": True,
            "target": {"template": "default"} if kind == "census" else {"name": "chat.corpus.run"}}
    base.update(kw)
    return core.normalize(base, now=NOW)


def test_no_census_while_an_exclusive_cap_runs():
    due, why = core.is_due(_rec("census"), NOW, {"exclusive_running": 1})
    assert not due and "still using compute" in why
    assert core.is_due(_rec("census"), NOW, {"exclusive_running": 0})[0]


def test_a_cap_waits_for_agent_loops_exclusive_or_not():
    assert core.is_due(_rec("cap"), NOW, {"loops_running": 1}) == (False, "an agent loop is running")
    assert core.is_due(_rec("cap", exclusive=False), NOW, {"loops_running": 1}) == (False, "an agent loop is running")
    assert core.is_due(_rec("cap"), NOW, {"loops_running": 0})[0]


def test_cap_runs_in_the_background_not_in_the_tick():
    i = SRC.index('    if kind == "cap":')
    seg = SRC[i:i + 900]
    assert "_in_background(run_id, rec, _call(" in seg and '"background": True' in seg
    assert "return await _call(t.get(\"name\"" not in seg
    assert 'state["exclusive_running"] = len(_EXCLUSIVE_BG)' in SRC
    assert "t.add_done_callback(lambda _t: _EXCLUSIVE_BG.pop(run_id, None))" in SRC
