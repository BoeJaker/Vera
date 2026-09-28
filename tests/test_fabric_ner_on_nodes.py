"""The fabric's entity NER uses the nodes' OntoNotes NER.

2026-09-28: neither spaCy nor GLiNER was installed on the host, so the entity
graph ran on capitalisation heuristics while every node served a real NER model
from the shared store."""
import asyncio

import pytest

import Vera.vera.fabric.fabric_web_acquisition as wa
from Vera.vera.fabric import ner_node_core as core
import Vera.vera.capability_orchestration as orch

pytestmark = pytest.mark.critical

TEXT = "Tim Cook met Angela Merkel in Berlin in 2023 to discuss 3 projects."


def _ents():
    # what a node returns: RoBERTa words carry a leading space; offsets are exact
    return [{"entity": "PERSON", "word": " Tim Cook", "score": 0.99, "start": 0, "end": 8},
            {"entity": "PERSON", "word": " Angela Merkel", "score": 0.98, "start": 13, "end": 26},
            {"entity": "GPE", "word": " Berlin", "score": 0.97, "start": 30, "end": 36},
            {"entity": "DATE", "word": " 2023", "score": 0.9, "start": 40, "end": 44},
            {"entity": "CARDINAL", "word": " 3", "score": 0.9, "start": 56, "end": 57}]


def test_mapping_uses_the_page_text_and_the_ontonotes_table():
    rows = core.node_entities(TEXT, _ents())
    assert [(n, t) for n, t, _p, _c in rows] == [
        ("Tim Cook", "person"), ("Angela Merkel", "person"), ("Berlin", "location"),
        ("2023", "date")]                       # CARDINAL dropped
    # no offsets: fall back to finding the word; unfindable -> skipped
    rows = core.node_entities(TEXT, [{"entity": "B-ORG", "word": "Berlin"},
                                     {"entity": "ORG", "word": "Nowhere Inc"}])
    assert rows == [("Berlin", "organisation", 30, 0.8)]
    # the host's spaCy path reads the same table
    assert wa._SPACY_TYPE is core.ONTONOTES_TYPE


def _with_fake_ner(monkeypatch, result):
    calls = []

    async def fake_ner(text="", task="ner", trace_id=None):
        calls.append((len(text), task))
        return result

    monkeypatch.setitem(orch.CAPABILITY_REGISTRY, "nlp.ner", {"func": fake_ner, "raw": fake_ner})
    monkeypatch.setenv("FABRIC_NER_BACKEND", "node")
    monkeypatch.setattr(wa, "_NER_STATE", {"init": False, "kind": "heuristic", "obj": None})
    return calls


def test_extraction_on_the_ner_thread_asks_the_nodes(monkeypatch):
    calls = _with_fake_ner(monkeypatch, {"ok": True, "entities": _ents()})
    ents = asyncio.run(wa.ner_offload(wa._extract_entities_from_text, TEXT, "text"))
    assert calls == [(len(TEXT), "ner")]
    got = {(e["name"], e["type"]) for e in ents}
    assert {("Tim Cook", "person"), ("Angela Merkel", "person"), ("Berlin", "location")} <= got
    assert wa._NER_STATE["kind"] == "node"


def test_no_node_serving_falls_back_to_the_heuristic(monkeypatch):
    _with_fake_ner(monkeypatch, {"error": "nlp.local is off and no node serves NLP"})
    ents = asyncio.run(wa.ner_offload(wa._extract_entities_from_text, TEXT, "text"))
    assert ents, "the heuristic still extracts something"


def test_never_blocks_the_event_loop_thread(monkeypatch):
    calls = _with_fake_ner(monkeypatch, {"ok": True, "entities": _ents()})

    async def on_the_loop():
        await wa.ner_offload(lambda: None)          # records the loop
        return wa._extract_entities_from_text(TEXT, "text")   # called ON the loop

    ents = asyncio.run(on_the_loop())
    assert calls == []                  # no node call from the loop thread
    assert isinstance(ents, list)       # heuristic answered instead
