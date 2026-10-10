"""Callable models as capability bindings (owner, 2026-10-10).

A capability declares the model(s) it serves (`@capability(..., serves=...)`),
each saying what it is FOR so an LLM can pick it; the registry of callable
models is a view over those plus the LLMs the routing system can pick
(`model.callable`). These pin the binding rules, the description section, the
view's joins, and the nlp.* bindings.
"""
import pytest

from vera import model_binding_core as mb
from vera.research import nlp_dispatch_core as ndc
from vera.research.nlp_models_core import NLP_SERVES

pytestmark = pytest.mark.critical

USES = "Find entities of any type named at call time."


def test_a_binding_must_say_what_it_is_for():
    ok = mb.binding("ner", "gliner", uses=USES, io_out="spans", placement="nlp")
    assert mb.validate(ok) == []
    assert any("uses is required" in e for e in mb.validate(mb.binding("ner", "gliner", io_out="spans")))
    assert any("family" in e for e in mb.validate(mb.binding("ner", "magic", uses=USES)))
    assert any("io.out" in e for e in mb.validate(mb.binding("ner", "nlp", uses=USES, io_out="poem")))
    assert any("tier" in e for e in mb.validate(mb.binding("x", "rules", uses=USES, tier=9)))


def test_ids_and_selectors():
    one, errs = mb.bind("nlp.qa", mb.binding("qa", "nlp", uses=USES, io_out="answer"))
    assert errs == [] and one[0]["id"] == "nlp.qa" and one[0]["cap"] == "nlp.qa"
    two, errs = mb.bind("nlp.ner", [
        mb.binding("ner", "nlp", uses=USES, selector={"task": "ner"}, io_out="spans"),
        mb.binding("ner", "gliner", uses=USES, selector={"task": "gliner"}, io_out="spans")])
    assert errs == [] and [b["id"] for b in two] == ["nlp.ner:ner", "nlp.ner:gliner"]
    assert two[1]["served_as"] == "gliner"          # the node's task key
    # several models need a selector each, or a caller could not pick one
    none, errs = mb.bind("x.y", [mb.binding("a", "nlp", uses=USES), mb.binding("b", "nlp", uses=USES)])
    assert none == [] and "selector" in errs[0]
    # a bad binding is dropped and reported; the good one stays
    kept, errs = mb.bind("x.y", [mb.binding("a", "nlp", uses=USES, selector={"task": "a"}),
                                 mb.binding("b", "nope", uses=USES, selector={"task": "b"})])
    assert [b["id"] for b in kept] == ["x.y:a"] and errs and "family" in errs[0]
    assert mb.bind("x.y", None) == ([], [])


def test_the_description_gains_a_models_section_once():
    bs, _ = mb.bind("nlp.ner", [mb.binding("ner", "gliner", uses=USES, limits="fixed types.",
                                           selector={"task": "gliner"}, io_out="spans")])
    d = mb.with_models("NER on the node tier.", bs)
    assert d.startswith("NER on the node tier.\nModels:\n- task='gliner': " + USES)
    assert "Not for: fixed types." in d
    assert mb.with_models(d, bs) == d               # idempotent
    assert mb.with_models("Plain.", []) == "Plain."


def test_the_view_joins_nodes_catalog_and_defaults():
    bs, _ = mb.bind("nlp.ner", [mb.binding("ner", "nlp", uses=USES, selector={"task": "ner"}, io_out="spans"),
                                mb.binding("ner", "gliner", uses=USES, selector={"task": "gliner"},
                                           io_out="spans", questions=["intel.entity_resolve"], tier=3)])
    reg = {"nlp.ner": {"models": bs, "mcp_expose": True}, "other.cap": {"description": "no models"}}
    nodes = [{"node_id": "cpu-246", "tasks": {"ner": {"model": "m/ner", "present": True, "loaded": True},
                                             "gliner": {"model": "m/gl", "present": True, "loaded": False}}},
             {"node_id": "cpu-247", "tasks": {"ner": {"model": "m/ner-override", "present": True,
                                                      "loaded": True}}}]
    rows = mb.callable_view(reg, defaults={"ner": "m/ner", "gliner": "m/gl"}, nodes=nodes,
                            catalog=[{"family": "gliner", "task": "", "model": "m/gl"}])
    by = {r["id"]: r for r in rows}
    assert set(by) == {"nlp.ner:ner", "nlp.ner:gliner"}
    assert by["nlp.ner:ner"]["model"] == "m/ner"
    assert by["nlp.ner:ner"]["loaded_on"] == ["cpu-246", "cpu-247"]
    assert by["nlp.ner:ner"]["node_models"] == ["m/ner", "m/ner-override"]   # a node override shows
    assert by["nlp.ner:gliner"]["loaded_on"] == [] and by["nlp.ner:gliner"]["present_on"] == ["cpu-246"]
    assert by["nlp.ner:gliner"]["installable"] == "gliner:m/gl"
    assert by["nlp.ner:gliner"]["uses"] == USES
    assert [r["id"] for r in mb.callable_view(reg, question="intel.entity_resolve")] == ["nlp.ner:gliner"]
    assert [r["id"] for r in mb.callable_view(reg, family="nlp")] == ["nlp.ner:ner"]


def test_llms_come_from_the_routing_system():
    inst = {"cpu-246": {"models": ["qwen3.5:9b", {"name": "nomic-embed-text"}],
                        "running": [{"name": "qwen3.5:9b"}]},
            "gpu-250": {"models": ["qwen3.5:9b"], "running": []}}
    rows = {r["model"]: r for r in mb.llm_rows(inst, {"qwen3.5:9b": ["planning", "chat"]})}
    q = rows["qwen3.5:9b"]
    assert q["cap"] == "llm.generate" and q["selector"] == {"model": "qwen3.5:9b"}
    assert q["uses"] == "Purpose tags: planning, chat"
    assert q["loaded_on"] == ["cpu-246"] and q["present_on"] == ["cpu-246", "gpu-250"]
    assert "untagged" in rows["nomic-embed-text"]["uses"]
    assert [r["model"] for r in mb.llm_rows(inst, {}, search="nomic")] == ["nomic-embed-text"]


def test_every_nlp_cap_binding_is_valid_and_names_a_served_task():
    ids = set()
    for cap, serves in NLP_SERVES.items():
        bs, errs = mb.bind(cap, serves)
        assert errs == [], errs
        assert bs, cap
        for b in bs:
            assert b["served_as"] in ndc.DEFAULT_MODELS, (cap, b["served_as"])
            assert b["served_as"] in ndc.TASK_KIND, (cap, b["served_as"])
            assert len(b["uses"]) >= 30, (cap, b["uses"])
            ids.add(b["id"])
    # every task the node serves is reachable through some binding
    assert {i.split(":")[-1] if ":" in i else i.split(".")[-1] for i in ids} >= set(ndc.DEFAULT_MODELS)
