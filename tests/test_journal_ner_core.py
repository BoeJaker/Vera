"""Roadmap B3 (compute-roles, 11 Oct 2026): node NER over each step's result; new named entities are
merged into its journal entry beside the run (per-run journal_ner, off by default)."""
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vera.dag import journal_ner_core as C  # noqa: E402

SRC = (ROOT / "vera" / "dag" / "dag_workshop_capabilities.py").read_text(encoding="utf-8")
pytestmark = pytest.mark.critical

NER = {"entities": [
    {"entity": "ORG", "word": "Redis Inc.", "score": 0.97},
    {"entity": "DATE", "word": "March 20, 2024", "score": 0.95},
    {"entity": "CARDINAL", "word": "3", "score": 0.99},
    {"entity": "ORG", "word": "Linux Foundation", "score": 0.4},
    {"entity": "PRODUCT", "word": "Valkey", "score": 0.9},
    {"entity": "ORG", "word": "redis inc.", "score": 0.9}]}


def test_text_of_bounds_summary_and_outputs():
    t = C.text_of({"summary": "Fetched the licence page.", "outputs": {"license": "RSALv2/SSPLv1"}})
    assert t == "Fetched the licence page.\nlicense: RSALv2/SSPLv1"
    assert len(C.text_of({"summary": "x" * 9000})) == C.TEXT_CHARS


def test_pick_keeps_types_scores_and_new_names():
    new = C.pick(NER, existing=["Valkey (PRODUCT)"])
    assert new == ["Redis Inc. (ORG)", "March 20, 2024 (DATE)"]
    assert C.pick({"error": "no node"}, []) == [] and C.pick(None, []) == []
    assert len(C.pick({"entities": [{"entity": "ORG", "word": "Org %d" % i, "score": 1} for i in range(30)]}, [])) == C.MAX_NEW


def test_merge_in_place_bounded():
    e = {"entities": ["a"] * (C.ENTITIES_CAP - 1)}
    assert C.merge(e, ["x (ORG)", "y (ORG)"]) == 1 and e["entities"][-1] == "x (ORG)"
    e2 = {}
    assert C.merge(e2, []) == 0 and "entities" not in e2


def test_wired_beside_the_run_and_off_by_default():
    assert 'v6_journal_ner = bool(body.get("journal_ner", False))' in SRC
    assert "journal_ner=v6_journal_ner," in SRC
    i = SRC.index("            if journal_ner:        # B3")
    assert "asyncio.create_task(_v6_journal_ner(" in SRC[i:i + 300]
    assert "await _v6_journal_ner(" not in SRC
    assert "for _t in _jner_tasks:" in SRC
