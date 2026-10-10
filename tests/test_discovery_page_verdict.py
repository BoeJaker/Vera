"""The crawl's drift-guard verdict on a page is a ledger decision (four-minds FM-7.5, the 200-page labelled set)."""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from vera.fabric import discovery_ledger_core as DL  # noqa: E402
from vera.minds import ledger_core as LC, training_state_core as TS  # noqa: E402

pytestmark = pytest.mark.critical


def test_a_kept_and_a_skipped_page_are_valid_decisions():
    kept = DL.page_decision("https://x.org/a", True, crawl_id="c1", relevance=0.71, coverage=0.66, depth=2)
    assert LC.check_decision(kept) == []
    assert (kept["question_id"], kept["answer"], kept["decider"], kept["mode"], kept["implementation"]) == (
        "discovery.page_on_topic", "on_topic", "rule", "rule", "rule:drift_guard")
    assert kept["labels"] == ["on_topic", "off_topic"] and kept["surface"] == "discovery" and kept["origin"] == "discovery"
    assert kept["run_id"] == "c1" and kept["state_ref"] == {"kind": "crawl_page", "id": DL.page_ref("https://x.org/a")}
    assert kept["detail"] == {"relevance": 0.71, "coverage": 0.66, "neg_hit": False, "depth": 2}
    skipped = DL.page_decision("https://x.org/b", False, llm=True, relevance=0.3, coverage=0.2, neg_hit=True, depth=1)
    assert LC.check_decision(skipped) == []
    assert (skipped["answer"], skipped["decider"], skipped["mode"], skipped["implementation"]) == (
        "off_topic", "llm", "llm_only", "llm:drift")


def test_the_inputs_are_what_the_guard_saw_and_discovery_inputs_are_kept():
    i = DL.page_inputs("valkey fork", "Valkey 8", "x" * 2000, "https://x.org/a", relevance="0.5", coverage=None)
    assert i["topic"] == "valkey fork" and len(i["excerpt"]) == DL.EXCERPT_CHARS and i["relevance"] == 0.5
    assert i["coverage"] is None and i["neg_hit"] is False
    assert "discovery" in TS.enabled_surfaces(None) and "loop" in TS.enabled_surfaces(None)
    assert TS.wanted({"surface": "discovery"}, TS.enabled_surfaces(None))


def test_the_guard_records_every_verdict():
    src = open(os.path.join(ROOT, "vera", "fabric", "discovery.py"), encoding="utf-8").read()
    guard = src[src.index("if depth > 0 and drift_guard:"):src.index('reason="off_topic_drift",')]
    assert guard.count("_record_page_verdict(") == 2          # the kept page and the guard's verdict
    assert "llm=_asked_llm" in guard and "_asked_llm = True" in guard
    rec = src[src.index("def _record_page_verdict("):src.index("async def _crawl_key_lock(")]
    assert "create_task(fn(row=d))" in rec and "_TSR.put(d," in rec and "except Exception" in rec
