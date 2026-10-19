"""No scheduled run starts while another is still using compute; the queue never starts beside a census (19 Oct 2026).

Owner: "make sure there are no clashes and scheduled items cant start untill the previous has completed/is no longer
using compute. forbid the gpu for the background judges. fix the chat model so it calls the :9b."
"""
import os
from datetime import datetime, timezone

import pytest

from vera.evolve import schedule_core as core

pytestmark = pytest.mark.critical

ROOT = os.path.join(os.path.dirname(__file__), "..")
NOW = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)


def _rec(i, kind="cap", exclusive=False):
    return {"id": i, "kind": kind, "enabled": True, "once_at": "2026-10-19T11:00:00Z", "exclusive": exclusive,
            "target": {}}


def test_a_non_exclusive_entry_waits_for_a_running_scheduled_run():
    ok, why = core.is_due(_rec("a"), NOW, {"scheduled_running": 1})
    assert not ok and "still using compute" in why


def test_a_non_exclusive_entry_waits_for_a_census():
    ok, why = core.is_due(_rec("a"), NOW, {"census_running": True})
    assert not ok and why == "census in flight"


def test_a_census_waits_for_any_scheduled_run():
    ok, why = core.is_due(_rec("c", kind="census"), NOW, {"scheduled_running": 1})
    assert not ok and "still using compute" in why


def test_one_start_per_tick_whatever_the_exclusive_flag():
    plan = core.plan_tick([_rec("a"), _rec("b"), _rec("c")], NOW, {})
    assert [p["schedule_id"] for p in plan] == ["a"]


def test_a_claude_session_request_uses_no_compute():
    assert not core.uses_compute({"kind": "claude_session"}) and core.uses_compute({"kind": "cap"})


def test_every_compute_run_is_counted_and_the_queue_has_a_hard_hold():
    sc = open(os.path.join(ROOT, "vera", "evolve", "schedule_capabilities.py"), encoding="utf-8").read()
    assert 'state["scheduled_running"] = len(_COMPUTE_BG)' in sc and "_COMPUTE_BG[run_id]" in sc
    assert "svc.register_hard_hold(_queue_hard_hold)" in sc
    q = open(os.path.join(ROOT, "vera", "idle_queue_service.py"), encoding="utf-8").read()
    d = q[q.index("async def drain_once("):]
    assert d.index("hold = await hard_hold()") < d.index("if held:\n        # A reserved window")


def test_judges_stop_for_a_census_and_the_default_model_is_the_9b():
    j = open(os.path.join(ROOT, "vera", "minds", "judge.py"), encoding="utf-8").read()
    assert '(await _raw("census.live")).get("active")' in j
    c = open(os.path.join(ROOT, "vera", "config.py"), encoding="utf-8").read()
    assert '"jaahas/qwen3.5-uncensored:9b")' in c
