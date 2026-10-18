"""Four-minds capabilities that write declare a Capability Contract v2 (backbone review 11 Oct 2026, W1-03/W1-05)."""
import os
import re
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from vera import capability_contract_core as CC  # noqa: E402
from vera.minds import contract_core as MC  # noqa: E402

pytestmark = pytest.mark.critical

WRITERS = {
    "vera/minds/ledger_capabilities.py": ("decide.record", "decide.ledger.import"),
    "vera/minds/tool_shortlist.py": ("caps.shortlist.index", "caps.shortlist.replay"),
    "vera/minds/training_state.py": ("decide.purge",),
    "vera/minds/switchboard_capabilities.py": ("minds.switch.set",),
    "vera/fabric/known_item.py": ("retrieval.known_items.build",),
    "vera/fabric/page_search.py": ("discovery.recall_index.sync",),
    "vera/intel/intel_capabilities.py": ("intel.decisions.backfill_inputs",),
}


def test_the_builder_covers_every_gate_dimension_with_known_effects():
    c = MC.contract("x.y", effects=["read", "write", "model"], idempotency="idempotent")
    for dim in CC.GATE_REQUIRED_DIMENSIONS:
        if dim in ("contract",):
            continue
        assert dim in c, dim
        if isinstance(c[dim], dict) and "status" in c[dim]:
            assert c[dim]["status"] != "unknown", dim
    assert set(c["effects"]) <= CC.EFFECTS and c["owner"] == "four-minds" and c["lifecycle"] in CC.LIFECYCLES


@pytest.mark.parametrize("rel,caps", sorted(WRITERS.items()))
def test_each_writer_declares_one(rel, caps):
    src = open(os.path.join(ROOT, rel), encoding="utf-8").read()
    for cap in caps:
        i = src.index('"%s"' % cap, src.index("@capability(", max(0, src.index('"%s"' % cap) - 200)))
        head = src[i:i + 400]
        assert "contract=_MC.contract(" in head, cap
        effects = re.search(r"effects=\[([^\]]*)\]", head).group(1)
        assert {e.strip().strip('"') for e in effects.split(",")} <= CC.EFFECTS, cap


def test_the_contract_gate_passes_a_four_minds_writer():
    entry = {"contract": MC.contract("decision.record", effects=["write"], idempotency="idempotent"),
             "schema": {"type": "object", "properties": {"row": {"type": "object"}}, "required": ["row"]},
             "description": "Record one decision row."}
    gate = CC.gate_contracts([CC.project_contract("decide.record", entry)])
    assert gate["ok"], gate["issues"]


READERS = {
    "vera/fabric/memory_retrieval.py": ("memory.seek", "memory.read", "memory.map", "memory.browse"),
    "vera/minds/switchboard_capabilities.py": ("minds.switch.list", "minds.switch.get", "minds.effective",
                                               "minds.validate"),
    "vera/minds/ledger_capabilities.py": ("decide.ledger", "decide.ledger.stats"),
    "vera/minds/label_capabilities.py": ("decide.label.queue",),
    "vera/minds/training_state.py": ("decide.training_state",),
    "vera/minds/judge.py": ("decide.shadow.report",),
    "vera/minds/sense_capabilities.py": ("sense.frame", "sense.stats"),
    "vera/minds/tool_shortlist.py": ("caps.shortlist", "caps.shortlist.report", "caps.shortlist.chat_report"),
    "vera/fabric/page_search.py": ("discovery.recall_index.status", "discovery.recall_index.search"),
    "vera/fabric/known_item.py": ("retrieval.known_items.run",),
    "vera/canvas/enrich_capabilities.py": ("canvas.surface_rank.report",),
    "vera/minds/chat_corpus.py": ("chat.corpus.last",),
}


@pytest.mark.parametrize("rel,caps", sorted(READERS.items()))
def test_each_reader_declares_one(rel, caps):
    test_each_writer_declares_one(rel, caps)


@pytest.mark.parametrize("rel", sorted(set(WRITERS) | set(READERS)))
def test_the_import_never_splits_stacked_decorators(rel):
    src = open(os.path.join(ROOT, rel), encoding="utf-8").read()
    i = src.index("    from Vera.vera.minds import contract_core as _MC")
    before = src[:i].rstrip().rsplit("\n", 2)[-2]
    assert not before.startswith("@"), (rel, before)
