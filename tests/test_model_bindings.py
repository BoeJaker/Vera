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


def test_the_view_preserves_node_reported_model_package_identity():
    bindings, _ = mb.bind("nlp.ner", [mb.binding("ner", "gliner", uses=USES,
                                                  selector={"task": "gliner"}, io_out="spans")])
    nodes = [{"node_id": "cpu-246", "tasks": {"gliner": {
        "model": "urchade/gliner_multi-v2.1", "present": True, "loaded": False,
        "model_package": {"package_id": "pkg:gliner-multi-v2.1"},
    }}}]
    row = mb.callable_view({"nlp.ner": {"models": bindings}}, nodes=nodes)[0]
    assert row["package_id"] == "pkg:gliner-multi-v2.1"
    assert row["package_ids"] == ["pkg:gliner-multi-v2.1"]
    assert row["nodes"]["cpu-246"]["package_id"] == "pkg:gliner-multi-v2.1"
    assert row["package_conflict"] is False


def test_llms_come_from_the_routing_system():
    inst = {"cpu-246": {"models": ["qwen3.5:9b", {"name": "nomic-embed-text"}],
                        "running": [{"name": "qwen3.5:9b"}]},
            "gpu-250": {"models": ["qwen3.5:9b"], "running": []}}
    rows = {r["model"]: r for r in mb.llm_rows(inst, {"qwen3.5:9b": ["planning", "chat"]})}
    q = rows["qwen3.5:9b"]
    assert q["cap"] == "llm.generate" and q["selector"] == {"model": "qwen3.5:9b"}
    assert q["uses"].startswith("Tagged for: planning, chat.")       # the operator's tags lead
    assert q["loaded_on"] == ["cpu-246"] and q["present_on"] == ["cpu-246", "gpu-250"]
    # nothing known yet: the row says so, and how to fix it
    assert "No facts or tags yet" in mb.llm_rows(inst, {}, search="qwen")[0]["uses"]
    assert [r["model"] for r in mb.llm_rows(inst, {}, search="nomic")] == ["nomic-embed-text"]
    # facts and measured speed write the description; an embedder is not a generator
    rows = {r["model"]: r for r in mb.llm_rows(
        inst, {}, facts={"qwen3.5:9b": {"family": "qwen35", "params": "9.7B", "quant": "Q4_K_M",
                                         "ctx": 262144, "abilities": ["completion", "tools", "vision"]},
                         "nomic-embed-text": {"abilities": ["embedding"]}},
        stats=[{"model": "qwen3.5:9b", "instance": "gpu-250", "ema_tps": 41.2, "n": 9},
               {"model": "qwen3.5:9b", "instance": "cpu-246", "ema_tps": 7.9, "n": 3}])}
    q = rows["qwen3.5:9b"]
    assert "reads images" in q["uses"] and "native tool calling" in q["uses"] and "256k" in q["uses"]
    assert "Measured 41 tok/s on gpu-250 (cpu-246 8)" in q["uses"], q["uses"]
    assert q["tps"] == {"gpu-250": 41.2, "cpu-246": 7.9}
    n = rows["nomic-embed-text"]
    assert n["task"] == "embed" and n["io"]["out"] == "vector" and "cannot generate" in n["limits"]


def test_llm_descriptions_from_facts():
    uses, limits = mb.llm_describe(
        "qwen3.6:35b-a3b", {"family": "qwen3moe", "params": "35.1B", "quant": "Q4_K_M", "ctx": 262144,
                            "abilities": ["completion", "tools", "thinking"]})
    assert "mixture-of-experts with 3B active" in uses and "a thinking mode" in uses and limits == ""
    uses, limits = mb.llm_describe("qwen3:0.6b", {"params": "751.63M", "ctx": 8192,
                                                  "abilities": ["completion"]}, speeds={"cpu-247": 3.1})
    assert "small" in limits and "no native tool calling" in limits and "short context" in limits
    assert "slow here" in limits
    f = mb.show_facts({"details": {"family": "llama", "parameter_size": "3.2B", "quantization_level": "Q8_0"},
                       "model_info": {"general.architecture": "llama", "llama.context_length": 131072},
                       "capabilities": ["tools", "completion"]})
    assert f == {"family": "llama", "params": "3.2B", "quant": "Q8_0", "ctx": 131072,
                 "abilities": ["completion", "tools"]}


def test_selectors_give_the_options_and_what_each_picks():
    assert mb.selector_values(NLP_SERVES["nlp.ner"], "task") == ["ner", "ner_multi", "gliner", "spacy"]
    assert mb.selector_values(NLP_SERVES["nlp.qa"], "task") == []
    bs, _ = mb.bind("nlp.ner", NLP_SERVES["nlp.ner"])
    frag = mb.selector_schema(bs)["task"]
    assert frag["enum"] == ["ner", "ner_multi", "gliner", "spacy"]
    # "e.g." is not the end of a sentence
    assert "gliner - Zero-shot entity extraction for ANY entity types named at call time (labels), e.g" \
        in frag["description"] and "e.g;" not in frag["description"]
    # the caps check `task` against the declared selectors, not a list of their own
    import os
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "vera", "research", "nlp_capabilities.py"), encoding="utf-8").read()
    assert "if task not in _NER_TASKS:" in src and "if task not in _CLASSIFY_TASKS:" in src
    assert '("ner", "ner_multi", "gliner", "spacy")' not in src


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


def _nlp_rows():
    reg = {c: {"models": mb.bind(c, s)[0]} for c, s in NLP_SERVES.items()}
    return mb.callable_view(reg)


def test_measured_latency_per_model_and_per_shared_endpoint():
    rows = _nlp_rows() + mb.llm_rows({"cpu-246": {"models": ["qwen3:4b"]}}, {})
    now = 1_000_000.0
    recs = [{"service": "nlp", "kind": "ner", "duration_s": d, "node": "cpu-246", "end": now - 60}
            for d in (0.1, 0.2, 0.3, 0.4, 2.0)]
    recs += [{"service": "nlp", "kind": "langid", "duration_s": 0.05, "node": "cpu-247", "end": now - 60,
              "status": 500, "error": "boom"},
             {"service": "ollama", "model": "qwen3:4b", "duration_s": 3.0, "node": "cpu-246", "end": now - 60},
             {"service": "nlp", "kind": "qa", "duration_s": 9.0, "end": now - 90000}]     # outside 24 h
    by = {r["id"]: r for r in mb.measure(rows, recs, now=now)}
    m = by["nlp.ner:gliner"]["measured"]
    assert m["calls"] == 5 and m["p50_s"] == 0.3 and m["p95_s"] == 2.0
    assert "shared" in m["scope"]                    # ner / gliner / spacy all call /ner
    assert by["nlp.langid"]["measured"]["error_rate"] == 1.0
    assert by["nlp.langid"]["measured"]["scope"] == "model"
    assert by["llm.generate:qwen3:4b"]["measured"]["calls"] == 1
    assert by["nlp.qa"]["measured"] == {"calls": 0}


def test_pick_ranks_models_by_what_they_are_for():
    rows = _nlp_rows()
    top = lambda q: mb.pick(rows, q, top_k=1)[0]["id"]
    assert top("find dates and amounts of money in an english news article") == "nlp.ner:ner"
    assert top("extract product names and software mentioned") == "nlp.ner:gliner"
    assert top("is this review positive or negative") == "nlp.classify:sentiment3"
    assert top("reorder search results by relevance to a query") == "nlp.rerank"
    assert top("answer a question from a passage") == "nlp.qa"
    # meaning, when vectors are given, outranks shared words
    vecs = {r["id"]: [0.0, 1.0] for r in rows}
    vecs["nlp.zeroshot"] = [1.0, 0.0]
    best = mb.pick(rows, "sort tickets into billing or outage", need_vec=[1.0, 0.0], row_vecs=vecs)[0]
    assert best["id"] == "nlp.zeroshot" and best["score"] > 0
    # a model loaded now beats an equal one that is not; errors cost
    a = {"id": "a", "uses": "translate text", "loaded_on": ["n1"]}
    b = {"id": "b", "uses": "translate text", "loaded_on": []}
    assert [r["id"] for r in mb.pick([b, a], "translate text")] == ["a", "b"]
    c = dict(a, id="c", measured={"error_rate": 1.0})
    assert mb.pick([c, b], "translate text")[0]["id"] == "b"


def test_the_description_section_is_compact():
    long = mb.binding("x", "nlp", uses="First sentence says what it is for. " + "More detail. " * 30,
                      limits="One limit. Another limit.")
    d = mb.describe(mb.bind("c.x", long)[0])
    assert d == "Models:\n- First sentence says what it is for. Not for: One limit."


def test_media_and_worldview_vocabulary():
    for fam in ("whisper", "kokoro", "coqui", "diffusers", "controlnet", "rembg", "esrgan", "jepa"):
        b = mb.binding("x", fam, uses="A model that does a particular thing.", io_in="audio", io_out="image")
        assert mb.validate(b) == [], fam


def test_pick_reads_the_direction_and_the_language_of_the_need():
    stt = {"id": "stt", "uses": "Transcribe speech audio to text.", "io": {"in": "audio", "out": "text"}}
    tts = {"id": "tts", "uses": "Speech audio from text.", "io": {"in": "text", "out": "audio"}}
    assert mb.pick([tts, stt], "turn this voice memo into text")[0]["id"] == "stt"
    assert mb.pick([stt, tts], "read this paragraph out as speech")[0]["id"] == "tts"
    en = {"id": "en", "uses": "Named entities in English text.", "io": {"in": "text", "out": "spans"}}
    multi = {"id": "multi", "uses": "Named entities in ten languages.", "io": {"in": "text", "out": "spans"}}
    assert mb.pick([en, multi], "named entities in a german article")[0]["id"] == "multi"
    assert mb.pick([multi, en], "named entities in an english article")[0]["id"] == "en"


def test_placement_follows_the_model_but_never_grants_a_node():
    from vera.workers import worker_placement_core as wp
    saved = dict(wp.MODEL_PLACEMENT), dict(wp.VETTED)
    try:
        wp.MODEL_PLACEMENT.clear()
        wp.set_vetted({})
        # a model in the host process keeps its cap on the host, even when vetted
        wp.set_model_placement("demo.encode", ["host"])
        wp.set_vetted({"demo.encode": ("extended", "evidence")})
        assert wp.placement("demo.encode") == ("host", "its model runs in the host process (serves=)")
        # a binding alone does not make a cap node-eligible
        wp.set_model_placement("demo.knn", ["nlp"])
        assert wp.placement("demo.knn")[0] == "host" and wp.class_of("demo.knn", env={}) == ""
        # once vetted, an NLP-node model goes to the nlp class, not the catch-all
        wp.set_vetted({"demo.knn": ("extended", "evidence")})
        assert wp.class_of("demo.knn", env={}) == "nlp"
        wp.set_model_placement("demo.img", ["gpu"])
        wp.set_vetted({"demo.knn": ("extended", "e"), "demo.img": ("extended", "e")})
        assert wp.class_of("demo.img", env={}) == "media"
        # bindings that disagree (or say 'any') record nothing
        wp.set_model_placement("demo.mixed", ["nlp", "gpu"])
        wp.set_model_placement("demo.any", ["any"])
        assert "demo.mixed" not in wp.MODEL_PLACEMENT and "demo.any" not in wp.MODEL_PLACEMENT
        # the operator's brake still wins
        assert wp.placement("demo.knn", host_only=["demo"])[0] == "host"
    finally:
        wp.MODEL_PLACEMENT.clear(); wp.MODEL_PLACEMENT.update(saved[0]); wp.set_vetted(saved[1])


def test_an_embedder_is_not_picked_to_produce_text():
    stt = {"id": "stt", "uses": "Transcribe speech audio to text.", "io": {"in": "audio", "out": "text"}}
    emb = {"id": "emb", "uses": "Text embeddings.", "io": {"in": "text", "out": "vector"}}
    zs = {"id": "zs", "uses": "Classify text against labels.", "io": {"in": "text+labels", "out": "labels"}}
    ranked = [r["id"] for r in mb.pick([emb, zs, stt], "turn this voice memo into text")]
    assert ranked[0] == "stt" and ranked.index("emb") > ranked.index("zs")


def test_a_need_to_write_or_plan_picks_a_generator():
    rows = _nlp_rows() + mb.llm_rows(
        {"cpu-246": {"models": ["qwen3-coder:30b", "qwen3.5:9b"]}}, {},
        facts={"qwen3-coder:30b": {"family": "qwen3moe", "params": "30.5B", "abilities": ["completion", "tools"]},
               "qwen3.5:9b": {"family": "qwen35", "params": "8.95B", "abilities": ["completion", "tools"]}})
    top = lambda q: mb.pick(rows, q, top_k=1)[0]["id"]
    assert top("write python code for a web scraper") == "llm.generate:qwen3-coder:30b"
    assert top("plan a multi-step research task").startswith("llm.generate:")
    # extraction still goes to the extractors
    assert top("find dates and amounts of money in an english news article") == "nlp.ner:ner"
    uses, _ = mb.llm_describe("qwen3-coder:30b", {"params": "30.5B"})
    assert uses.startswith("Writes, edits, reviews and explains code")
