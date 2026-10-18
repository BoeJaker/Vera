"""The agent work registry (compute-roles L1, owner 11 Oct 2026): agent triggers + owner requests, a read
model for the UI's work windows that never marks the box busy."""
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vera.agents import agent_work_core as C  # noqa: E402

CAPS = (ROOT / "vera" / "agents" / "agent_work_capabilities.py").read_text(encoding="utf-8")
ORCH = (ROOT / "vera" / "capability_orchestration.py").read_text(encoding="utf-8")
pytestmark = pytest.mark.critical
T0 = C._epoch("2026-10-12T01:00:00Z")


def test_normalize_validates_and_keeps_history():
    e = C.normalize({"kind": "session_cron", "title": "resume compute-roles", "session": "vera-fb",
                     "when": "2026-10-12T03:13:00Z", "board_items": "a, b"}, now=T0)
    assert e["status"] == "armed" and e["board_items"] == ["a", "b"] and e["id"].startswith("w")
    upd = C.normalize({"id": e["id"], "status": "running"}, prev=e, now=T0 + 10)
    assert upd["title"] == "resume compute-roles" and upd["created_at"] == e["created_at"]
    for bad in ({"kind": "x", "title": "t"}, {"kind": "host_job"}, {"kind": "request", "title": "t"},
                {"kind": "one_shot", "title": "t", "when": "tomorrow"}, {"kind": "one_shot", "title": "t", "status": "pending"}):
        with pytest.raises(ValueError):
            C.normalize(bad, now=T0)


def test_view_status_expires_silent_triggers():
    e = C.normalize({"kind": "session_cron", "title": "t", "when": "2026-10-12T01:00:00Z"}, now=T0 - 60)
    assert C.view_status(e, now=T0 + 3600) == "armed"
    assert C.view_status(e, now=T0 + C.STALE_AFTER_FIRE_S + 60) == "expired"
    beat = C.normalize({"id": e["id"], "status": "running"}, prev=e, now=T0 + 100)
    assert C.view_status(beat, now=T0 + C.STALE_AFTER_FIRE_S + 60) == "running"


def test_requests_pending_for_a_session_and_listing_order():
    r1 = C.normalize({"kind": "request", "title": "review board X", "session": "vera-fb", "when": "2026-10-12T00:30:00Z", "status": "pending"}, now=T0)
    r2 = C.normalize({"kind": "request", "title": "later", "session": "vera-fb", "when": "2026-10-12T05:00:00Z", "status": "pending"}, now=T0)
    r3 = C.normalize({"kind": "request", "title": "anyone", "session": "any", "status": "pending"}, now=T0)
    r4 = C.normalize({"kind": "request", "title": "other", "session": "vera-62", "status": "pending"}, now=T0)
    got = [e["title"] for e in C.pending_for([r1, r2, r3, r4], "vera-fb", now=T0)]
    assert got == ["anyone", "review board X"] or got == ["review board X", "anyone"]
    assert "later" not in got and "other" not in got
    lst = C.listing([r2, r1], now=T0)
    assert [e["title"] for e in lst] == ["review board X", "later"] and lst[0]["view_status"] == "pending"


def test_caps_registered_and_never_declare_busy():
    for cap in ("agents.work.register", "agents.work.heartbeat", "agents.work.list", "agents.work.request",
                "agents.work.pending", "agents.work.ack"):
        assert '"%s"' % cap in CAPS
    assert "sys.activity.declare" not in CAPS.replace("(sys.activity.declare does", "")
    assert 'os.path.join(_here, "agents/agent_work_capabilities.py"),' in ORCH
