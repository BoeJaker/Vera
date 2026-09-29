"""GLiNER and spaCy as node-tier engines (MASTER-TODO: the ollama nodes run every entity backend the fabric has;
EXPLODE.md A1), and every node backend as an Explode layer.

The server side is tested with fakes standing where the models load (the models live in the read-only store on
the nodes); the layer side with a fake nlp.ner / nlp.zeroshot / nlp.qa / nlp.embed in the registry."""
import asyncio
import os
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "edge"))
sys.path.insert(0, os.path.join(_ROOT, "vera", "research"))

import nlp_server as S                                   # noqa: E402
from vera.research import nlp_dispatch_core as C         # noqa: E402
from vera.research import explode_capabilities as X      # noqa: E402

TEXT = "Alice Carter, chief executive of Northwind Ltd, spoke in Bristol on Tuesday about the Contoso deal.\n\nBob Ng founded Contoso in 2015."


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def test_the_registry_names_both_engines_and_the_labels_the_fabric_uses():
    assert C.DEFAULT_MODELS["gliner"] == "urchade/gliner_medium-v2.1" and C.TASK_KIND["gliner"] == "gliner"
    assert C.DEFAULT_MODELS["spacy"] == "en_core_web_sm" and C.TASK_KIND["spacy"] == "spacy"
    assert "person" in C.GLINER_LABELS_DEFAULT and "government agency" in C.GLINER_LABELS_DEFAULT and len(C.GLINER_LABELS_DEFAULT) == 37


def test_model_presence_is_a_checkpoint_dir_for_gliner_and_an_importable_package_for_spacy(tmp_path, monkeypatch):
    monkeypatch.setattr(S, "MODEL_ROOT", str(tmp_path))
    assert S.model_present("gliner") is False
    d = tmp_path / C.model_slug(C.DEFAULT_MODELS["gliner"]); d.mkdir()
    (d / "gliner_config.json").write_text("{}")
    assert S.model_present("gliner") is True
    monkeypatch.setenv("VERA_NLP_MODEL_SPACY", "json")          # an importable name stands in for the pipeline package
    assert S.model_present("spacy") is True
    monkeypatch.setenv("VERA_NLP_MODEL_SPACY", "no_such_pipeline_xyz")
    assert S.model_present("spacy") is False


class _FakeGliner:
    def __init__(self):
        self.calls = []

    def predict_entities(self, chunk, labels, threshold=0.4):
        self.calls.append((chunk, list(labels), threshold))
        out = []
        for name, label in (("Alice Carter", "person"), ("Northwind Ltd", "company"), ("Bristol", "city"), ("Contoso", "company")):
            p = chunk.find(name)
            if p >= 0:
                out.append({"text": name, "label": label, "score": 0.93, "start": p, "end": p + len(name)})
        return out


class _Span:
    def __init__(self, text, label, start):
        self.text, self.label_, self.start_char, self.end_char = text, label, start, start + len(text)


class _FakeSpacy:
    def __call__(self, chunk):
        ents = []
        for name, label in (("Alice Carter", "PERSON"), ("Bristol", "GPE"), ("2015", "DATE"), ("14", "CARDINAL")):
            p = chunk.find(name)
            if p >= 0:
                ents.append(_Span(name, label, p))
        return type("Doc", (), {"ents": ents})()


def test_run_ner_serves_gliner_over_the_fabrics_labels_at_a_threshold_and_spacy_without_bare_numbers(monkeypatch):
    g = _FakeGliner()
    monkeypatch.setitem(S._RAW, "gliner", g)
    monkeypatch.setitem(S._RAW, "spacy", _FakeSpacy())
    out = S.run_ner(TEXT, task="gliner")
    assert out["ok"] and out["task"] == "gliner" and out["labels"] == C.GLINER_LABELS_DEFAULT and out["threshold"] == 0.4
    names = {(e["word"], e["entity"]) for e in out["entities"]}
    assert ("Alice Carter", "person") in names and ("Northwind Ltd", "company") in names and ("Contoso", "company") in names
    assert all(TEXT[e["start"]:e["end"]] == e["word"] for e in out["entities"]), "offsets are the document's"
    out2 = S.run_ner(TEXT, task="gliner", labels=["person"], threshold=0.7)
    assert out2["labels"] == ["person"] and g.calls[-1][1] == ["person"] and g.calls[-1][2] == 0.7
    sp = S.run_ner(TEXT + " Later, 14 people came.", task="spacy")
    words = {e["word"] for e in sp["entities"]}
    assert {"Alice Carter", "Bristol", "2015"} <= words and "14" not in words, "CARDINAL is dropped, as the fabric's spaCy path drops it"
    assert all(e["score"] == 0.85 for e in sp["entities"])


def test_the_http_body_carries_labels_and_threshold(monkeypatch):
    fastapi = pytest.importorskip("fastapi", reason="edge servers need fastapi")
    from fastapi.testclient import TestClient
    seen = {}

    def fake_run_ner(text, task="ner", max_chars=0, overlap=-1, labels=None, threshold=0.4):
        seen.update(task=task, labels=labels, threshold=threshold)
        return {"ok": True, "entities": []}
    monkeypatch.setattr(S, "run_ner", fake_run_ner)
    client = TestClient(S.build_app())
    r = client.post("/ner", json={"text": "x", "task": "gliner", "labels": ["person", "city"], "threshold": 0.55})
    assert r.status_code == 200 and seen == {"task": "gliner", "labels": ["person", "city"], "threshold": 0.55}


def test_every_node_backend_is_an_explode_layer(monkeypatch):
    """gliner / spacy / multilingual NER as layers with MATCHES to the fabric's cards and GLiNER's labels mapped to
    the diagram's kinds; genre as a verdict; claims as cards at their answer spans; paragraphs and their similarity."""
    saved = {k: dict(v) for k, v in X.LAYERS.items()}

    async def ner(ctx):
        ents = [{"name": "Alice Carter", "type": "named_entity", "normalised": "alice carter", "position": 0, "mention_count": 1, "confidence": 0.45}]
        ctx["ent_list"] = ents
        return {"cards": X._entity_cards(ents, ctx, "ner", "heuristic patterns")}
    monkeypatch.setitem(X.LAYERS["ner"], "fn", ner)

    async def fake_nlp_ner(text="", task="ner", labels=None, threshold=0.4, trace_id=None):
        if task == "gliner":
            assert labels and "government agency" in labels, "the layer hands the fabric's label set to the node"
            ents = [("Alice Carter", "person", 0.97), ("Northwind Ltd", "company", 0.9), ("Bristol", "city", 0.88)]
        elif task == "spacy":
            ents = [("Alice Carter", "PERSON", 0.85), ("Tuesday", "DATE", 0.85)]
        else:
            ents = [("Contoso", "ORG", 0.99)]
        return {"ok": True, "model": "fake-" + task, "node": "cpu-246",
                "entities": [{"entity": l, "word": w, "score": s, "start": text.find(w), "end": text.find(w) + len(w)} for w, l, s in ents]}

    async def fake_zeroshot(text="", labels=None, multi_label=False, trace_id=None):
        return {"ok": True, "model": "deberta", "labels": [{"label": "news report", "score": 0.81}, {"label": "opinion or commentary", "score": 0.1}]}

    async def fake_qa(question="", context="", trace_id=None):
        if question.startswith("Who"):
            p = context.find("Alice Carter") if "Alice" in context else context.find("Bob Ng")
            return {"ok": True, "model": "squad2", "answer": context[p:p + (12 if "Alice" in context else 6)], "score": 0.8, "start": p, "end": p + 12}
        return {"ok": True, "answer": "", "score": 0.0, "start": None}

    async def fake_embed(texts=None, trace_id=None):
        return {"ok": True, "model": "minilm", "node": "cpu-247", "embeddings": [[1.0, 0.0], [0.9, 0.436]]}

    monkeypatch.setitem(X.CAPABILITY_REGISTRY, "nlp.ner", {"func": fake_nlp_ner})
    monkeypatch.setitem(X.CAPABILITY_REGISTRY, "nlp.zeroshot", {"func": fake_zeroshot})
    monkeypatch.setitem(X.CAPABILITY_REGISTRY, "nlp.qa", {"func": fake_qa})
    monkeypatch.setitem(X.CAPABILITY_REGISTRY, "nlp.embed", {"func": fake_embed})
    out = _run(X.explode_prose(text=TEXT, layers=["ner", "ner.gliner", "ner.spacy", "ner.multi", "rel.cooccur", "cls.genre", "qa.claims", "paragraphs", "sim.embed"]))
    cards = {(c["layer"], c["title"]): c for c in out["cards"]}
    assert cards[("ner.gliner", "Northwind Ltd")]["kind"] == "org" and cards[("ner.gliner", "Bristol")]["kind"] == "location", "GLiNER's labels map to the diagram's kinds"
    assert cards[("ner", "Alice Carter")]["kind"] == "person" and "retyped" in cards[("ner", "Alice Carter")]["badges"], "the exact-span gliner read retypes the generic fabric card"
    assert ("ner.spacy", "Tuesday") in cards and cards[("ner.spacy", "Tuesday")]["kind"] == "date" and ("ner.multi", "Contoso") in cards
    m = [e for e in out["edges"] if e["kind"] == "MATCHES" and e["layer"] in ("ner.gliner", "ner.spacy")]
    assert any(e["to"] == cards[("ner", "Alice Carter")]["id"] for e in m), "both node engines match the fabric's Alice"
    rows = {r["id"]: r for r in out["layers"]}
    assert rows["ner.gliner"]["where"] == "cpu-246" and rows["ner.gliner"]["by"] == "fake-gliner" and rows["ner.spacy"]["count"] > 0
    genre = [a for a in out["assessments"] if a["key"] == "genre"][0]
    assert genre["label"] == "genre · news report" and genre["score"] == 0.81 and "deberta" in genre["by"]
    claims = [c for c in out["cards"] if c["layer"] == "qa.claims"]
    assert claims and all(c["kind"] == "claim" and TEXT[c["span"]["start"]:c["span"]["end"]] == c["title"] for c in claims) and claims[0]["fields"][0]["k"] == "question"
    paras = [c for c in out["cards"] if c["layer"] == "paragraphs"]
    assert len(paras) == 2 and paras[0]["group"] == "p1" and TEXT[paras[0]["span"]["start"]:paras[0]["span"]["end"]].startswith("Alice Carter")
    sim = [e for e in out["edges"] if e["layer"] == "sim.embed"]
    assert len(sim) == 1 and sim[0]["kind"] == "MATCHES" and sim[0]["resolution"] == "exact" and sim[0]["from"] == paras[0]["id"] and sim[0]["label"].startswith("similar · 0.9")
    # the non-default node layers stay off unless asked: the default run has none of them
    dflt = _run(X.explode_prose(text=TEXT, layers=None))
    assert not any(l["on"] and l["id"] in ("ner.gliner", "ner.spacy", "ner.multi", "cls.genre", "qa.claims", "paragraphs", "sim.embed") for l in dflt["layers"])
    ids = [l["id"] for l in X.layer_list()]
    assert ids.index("ner.gliner") < ids.index("rel.typed") and ids.index("paragraphs") < ids.index("sim.embed"), "an engine runs before the relations that read it"
    for k, v in saved.items():
        X.LAYERS[k].update(v)


def test_the_host_cap_routes_gliner_and_spacy_to_a_node_only(monkeypatch):
    from vera.research import nlp_capabilities as N
    calls = []

    async def fake_node_only(cap, path, body):
        calls.append((cap, path, body)); return {"ok": True, "entities": []}
    monkeypatch.setattr(N, "_node_only", fake_node_only, raising=False)
    fn = X.CAPABILITY_REGISTRY.get("nlp.ner", {}).get("raw") or X.CAPABILITY_REGISTRY.get("nlp.ner", {}).get("func")
    if fn is None:
        pytest.skip("nlp.ner is not registered in this process (no orchestrator)")
    out = _run(fn(text="x", task="gliner", labels="person, city", threshold=0.5))
    assert out["ok"] and calls[-1][1] == "/ner" and calls[-1][2] == {"text": "x", "task": "gliner", "labels": ["person", "city"], "threshold": 0.5}
    assert "task must be" in _run(fn(text="x", task="nope"))["error"]
