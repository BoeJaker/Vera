"""Intel's nights, assessments, alerts, briefs and actions appear on Vera's unified
activity timeline: intel builds the events, the activity module composes them."""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vera.intel import intel_activity_core as act  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..")


def test_events_from_the_store_in_the_timeline_shape():
    evs = act.events(
        snapshots=[{"topic_id": "t1", "ts": "2026-10-08T23:10:00Z", "headline": "Rates bite", "confidence": "high",
                    "state": "s", "changed": ["a", "b"]}],
        reports=[{"id": "r1", "ts": "2026-10-09T00:38:00Z", "title": "Intel brief", "topic_ids": ["t1"],
                  "counts": {"topics": 3, "signals": 4}},
                 {"id": "r2", "ts": "2026-10-07T00:50:00Z", "title": "Old", "topic_ids": ["t2"], "counts": {}}],
        deliveries=[{"report_id": "r1", "channel": "telegram", "ts": "2026-10-09T06:00:00Z", "ok": True},
                    {"report_id": "r1", "channel": "print", "ts": "2026-10-09T06:00:00Z", "ok": False}],
        alerts=[{"ts": "2026-10-08T23:11:00Z", "topic": "t1", "why": "the headline moved", "headline": "Rates bite"}],
        actions=[{"token": "k", "status": "done", "label": "Research further", "created": "2026-10-09T08:00:00Z",
                  "finished": "2026-10-09T08:05:00Z", "topic_id": "t1"},
                 {"token": "o", "status": "offered", "created": "2026-10-09T08:00:00Z"}],
        night={"ts": "2026-10-08T23:39:38Z", "stats": {"topics": 12, "searched": 48, "new": 143, "assessed": 1}},
        names={"t1": "UK residential"})
    kinds = [e["kind"] for e in evs]
    assert kinds == ["intel.action", "intel.report", "intel.night", "intel.alert", "intel.assessment", "intel.report"]
    r1 = next(e for e in evs if e["ref"] == "r1")
    assert r1["status"] == "sent" and "sent to telegram" in r1["summary"] and "failed: print" in r1["summary"]
    assert r1["ui"]["url"] == "/intel/report/view/r1"
    assert next(e for e in evs if e["ref"] == "r2")["status"] == "built"
    a = next(e for e in evs if e["kind"] == "intel.assessment")
    assert a["title"] == "Assessed UK residential: Rates bite" and a["status"] == "high" and a["source"] == "intel"
    assert all(set(e) >= {"kind", "ts", "title", "summary", "status", "ref", "source", "ui"} for e in evs)
    # one topic: its own events and the briefs that cover it; no night batch
    only = act.events(snapshots=[{"topic_id": "t2", "ts": "1"}], reports=[{"id": "r1", "ts": "2", "topic_ids": ["t1"]}],
                      night={"ts": "3"}, topic_id="t1")
    assert [e["kind"] for e in only] == ["intel.report"]


def test_the_capability_and_the_activity_timeline(tmp_path):
    from test_intel_capabilities import _load
    mod, orch = _load(tmp_path)
    R = lambda n, **kw: asyncio.run(orch.CAPABILITY_REGISTRY[n]["func"](**kw))   # noqa: E731
    t = R("intel.topic.save", name="AI property valuation", brief="AI valuation in UK property")["topic"]
    R("intel.sweep", topic_id=t["id"])
    asyncio.run(mod.enrich_topic(mod.store().get_topic(t["id"])))
    asyncio.run(mod.snapshot_topic(mod.store().get_topic(t["id"])))
    evs = R("intel.activity", topic_id=t["id"])["events"]
    assert any(e["kind"] == "intel.assessment" and "Pilots are going live" in e["title"] for e in evs)
    src = open(os.path.join(ROOT, "vera", "activity", "activity_capabilities.py"), encoding="utf-8").read()
    assert 'CAPABILITY_REGISTRY.get("intel.activity")' in src and 'elif kind == "intel":' in src
    assert "events += await _intel_events(limit=min(lim, 25))" in src
    panel = open(os.path.join(ROOT, "vera", "intel", "intel_panel.html"), encoding="utf-8").read()
    assert '<vera-activity-timeline scope="intel"' in panel and "/ui/elements/activity_timeline.js" in panel
