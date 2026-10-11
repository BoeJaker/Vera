"""Known dated facts before planning (compute-roles P1b): intel.lookup's tracked signals become dated
evidence items and a planner KNOWN block, by the rules agreed with the intelligence unit on 11 Oct."""
import pathlib
import sys
from datetime import datetime, timezone

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vera.dag import intel_prefacts_core as C  # noqa: E402

SRC = (ROOT / "vera" / "dag" / "dag_workshop_capabilities.py").read_text(encoding="utf-8")
pytestmark = pytest.mark.critical

VIEW = {"tracked": True, "topic": {"id": "t1", "name": "UK interest rates"},
        "snapshot": {"headline": "Bank Rate held at 3.75%", "ts": "2026-10-09T08:00:00Z", "age_days": 2},
        "signals": [
            {"title": "Old", "url": "https://a/1", "published": "2026-09-20", "seen": "2026-09-21", "summary": "s1"},
            {"title": "Crawled", "url": "https://a/2", "published": "", "seen": "2026-10-10", "summary": "s2"},
            {"title": "Undated", "url": "https://a/3", "published": "", "seen": "", "summary": "s3"},
            {"title": "New", "url": "https://a/4", "published": "2026-10-08", "seen": "2026-10-08", "summary": "s4"},
            {"title": "No url", "url": "", "published": "2026-10-09"}]}


def test_latest_and_since():
    now = datetime(2026, 10, 11, tzinfo=timezone.utc)
    assert C.is_latest("What's the latest on Valkey?") and not C.is_latest("Explain how Redis works")
    assert C.since_for("latest Bank Rate news", now) == "2026-10-04" and C.since_for("Explain Redis", now) == ""


def test_items_tracked_only_dated_newest_first():
    its = C.items(VIEW, latest=True)
    assert [i["title"] for i in its] == ["Crawled", "New", "Old"]          # undated dropped for latest
    assert its[0]["published"] == "seen 2026-10-10" and its[1]["published"] == "2026-10-08"
    assert [i["n"] for i in its] == [1, 2, 3] and all(i["source"] == "intel" for i in its)
    assert "Undated" in [i["title"] for i in C.items(VIEW, latest=False)]
    assert C.items({"tracked": False, "matches": [{"name": "x"}]}, True) == []


def test_assessment_is_fresh_only_and_never_evidence():
    assert C.assessment(VIEW)["headline"] == "Bank Rate held at 3.75%"
    old = dict(VIEW, snapshot=dict(VIEW["snapshot"], age_days=9))
    assert C.assessment(old) is None
    b = C.block(C.items(VIEW, True), topic="UK interest rates", assess=C.assessment(VIEW))
    assert b.startswith("KNOWN (dated, from Vera's own tracking of 'UK interest rates'")
    assert "- [seen 2026-10-10] Crawled" in b and "INTEL ASSESSMENT (2026-10-09" in b and "not a source" in b
    assert C.block([], assess=None) == ""


def test_wired_only_when_asked_so_default_prompts_hold():
    assert 'v6_intel_prefacts = bool(body.get("intel_prefacts", False))' in SRC
    assert "intel_prefacts=v6_intel_prefacts," in SRC
    i = SRC.index("# P1b: known dated facts before planning")
    seg = SRC[i:i + 700]
    assert "if intel_prefacts and _prefacts_core is not None" in seg and "_style_note = (_pf_block" in seg
    assert seg.index("_style_note = (_pf_block") < SRC[i:].index("plan_note=_style_note")
    body = SRC[SRC.index("async def _v6_intel_prefacts"):][:2500]
    assert "_evidence_core.add(sid" in body and "_prefacts_core.assessment(view)" in body
