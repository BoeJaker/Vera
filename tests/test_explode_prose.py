"""nlp.explode.prose assembles the Explode contract from layers (EXPLODE.md §4, §7.1): every card carries the span
of its first mention in the RECORD's coordinates, every edge its resolution, every layer its receipt; a layer that
fails is its own receipt and never the diagram's; a layer that needs another is skipped when that one is off; a
slice keeps record coordinates and is marked partial; several records become lanes."""
import asyncio

import pytest

from vera.research import explode_capabilities as X

TEXT = ("Alice Carter, chief executive of Northwind Ltd, announced in Bristol in March 2024 that the company would "
        "acquire Contoso.\n\nThe acquisition, founded on a deal Bob Ng had shaped, was welcomed. She said the terms were "
        "fair.\n\nThe deal closes in June 2024.")


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


@pytest.fixture
def fake_layers(monkeypatch):
    """A fake NER that finds a fixed set of entities by text search, and a relation layer that joins two of them —
    so the assembly is tested without the fabric's engines."""
    saved = {k: dict(v) for k, v in X.LAYERS.items()}

    async def ner(ctx):
        ents = []
        for name, etype in (("Alice Carter", "person"), ("Northwind Ltd", "organisation"), ("Bristol", "location"),
                            ("March 2024", "date"), ("Contoso", "organisation"), ("Bob Ng", "person")):
            p = ctx["text"].find(name)
            if p >= 0:
                ents.append({"name": name, "type": etype, "normalised": name.lower(), "position": p, "mention_count": 1, "confidence": 0.9})
        ctx["ent_list"] = ents
        return {"cards": X._entity_cards(ents, ctx, "ner", "fake NER"), "by": "fake NER", "where": "test"}

    async def rel(ctx):
        by = {c["title"]: c["id"] for c in ctx["cards"]}
        edges = []
        if "Alice Carter" in by and "Northwind Ltd" in by:
            edges.append({"from": by["Alice Carter"], "to": by["Northwind Ltd"], "layer": "rel.typed", "kind": "RELATES", "label": "leads", "resolution": "exact", "by": "fake"})
        return {"edges": edges}

    async def boom(ctx):
        raise RuntimeError("the node is away")

    monkeypatch.setitem(X.LAYERS["ner"], "fn", ner)
    monkeypatch.setitem(X.LAYERS["rel.typed"], "fn", rel)
    monkeypatch.setitem(X.LAYERS["ner.node"], "fn", boom)
    yield
    for k, v in saved.items():
        X.LAYERS[k].update(v)


def test_paragraphs_and_sentences_keep_offsets():
    paras = X.paragraphs_of(TEXT, base=100)
    assert len(paras) == 3 and paras[0]["start"] == 100 and TEXT[paras[1]["start"] - 100:paras[1]["end"] - 100].startswith("The acquisition")
    assert paras[2]["text"] == "The deal closes in June 2024."
    sents = X.sentences_of(TEXT)
    assert len(sents) >= 4 and TEXT[sents[0]["start"]:sents[0]["end"]].strip().endswith("acquire Contoso.")
    assert X.paragraphs_of("one paragraph, no blank line")[0]["end"] == len("one paragraph, no blank line")


def test_a_passage_becomes_paragraph_groups_and_span_carrying_cards(fake_layers):
    out = _run(X.explode_prose(text=TEXT, layers=["ner", "rel.typed", "rel.cooccur"]))
    assert out["ok"] and out["kind"] == "prose" and out["layout"]["mode"] == "position" and out["source"]["partial"] is True
    assert [g["id"] for g in out["groups"]] == ["p1", "p2", "p3"] and all(g["kind"] == "paragraph" for g in out["groups"])
    cards = {c["title"]: c for c in out["cards"]}
    assert set(cards) == {"Alice Carter", "Northwind Ltd", "Bristol", "March 2024", "Contoso", "Bob Ng"}
    alice = cards["Alice Carter"]
    assert alice["group"] == "p1" and TEXT[alice["span"]["start"]:alice["span"]["end"]] == "Alice Carter" and alice["kind"] == "person"
    assert cards["Bob Ng"]["group"] == "p2" and cards["Northwind Ltd"]["kind"] == "org"
    assert all(c["span"]["start"] < c["span"]["end"] for c in out["cards"]), "no card without a span"
    kinds = {e["kind"] for e in out["edges"]}
    assert "RELATES" in kinds and "CO_OCCURS" in kinds
    assert all(e["resolution"] in ("exact", "heuristic") for e in out["edges"])
    co = [e for e in out["edges"] if e["kind"] == "CO_OCCURS"]
    assert co and all(e["resolution"] == "heuristic" for e in co)
    # the typed relation is not duplicated by co-occurrence
    typed = [(e["from"], e["to"]) for e in out["edges"] if e["kind"] == "RELATES"]
    assert not any((e["from"], e["to"]) in typed or (e["to"], e["from"]) in typed for e in co)
    assert out["source"]["text"] == TEXT and out["counts"]["cards"] == 6


def test_layer_receipts_a_failing_layer_and_a_layer_that_needs_another(fake_layers):
    out = _run(X.explode_prose(text=TEXT, layers=["ner", "ner.node", "rel.typed"]))
    rows = {r["id"]: r for r in out["layers"]}
    assert rows["ner"]["on"] and rows["ner"]["count"] == 6 and rows["ner"]["by"] == "fake NER"
    assert rows["ner.node"]["on"] and "the node is away" in rows["ner.node"]["error"] and rows["ner.node"]["count"] == 0
    assert rows["rel.cooccur"]["on"] is False and rows["langid"]["on"] is False
    assert out["counts"]["cards"] == 6, "a failing layer costs its own cards, not the diagram"
    # a relation layer without its NER is skipped with a note
    out2 = _run(X.explode_prose(text=TEXT, layers=["rel.typed"]))
    r = {x["id"]: x for x in out2["layers"]}["rel.typed"]
    assert r["on"] is False and "needs ner" in r["error"] and out2["counts"]["edges"] == 0


def test_a_record_slice_keeps_record_coordinates_and_records_become_lanes(fake_layers, monkeypatch):
    store = {"r1": {"id": "r1", "dataset_id": "d", "text": TEXT, "source_id": "s", "tags": ""},
             "r2": {"id": "r2", "dataset_id": "d", "text": "Contoso was founded by Bob Ng.", "source_id": "s", "tags": ""}}
    monkeypatch.setattr(X, "_read_record", lambda rid: store.get(rid))
    p2 = X.paragraphs_of(TEXT)[1]
    out = _run(X.explode_prose(record_id="r1", ranges=[[p2["start"], p2["end"]]], layers=["ner"]))
    assert out["source"]["record_id"] == "r1" and out["source"]["partial"] is True and out["layout"]["mode"] == "position"
    titles = {c["title"]: c for c in out["cards"]}
    assert set(titles) == {"Bob Ng"} and TEXT[titles["Bob Ng"]["span"]["start"]:titles["Bob Ng"]["span"]["end"]] == "Bob Ng"
    assert titles["Bob Ng"]["span"]["path"] == "r1" and out["groups"][0]["span"]["start"] == p2["start"]
    whole = _run(X.explode_prose(record_id="r1", layers=["ner"]))
    assert whole["layout"]["mode"] == "type" and whole["source"]["partial"] is False and whole["counts"]["cards"] == 6
    lanes = _run(X.explode_prose(record_ids=["r1", "r2"], layers=["ner"]))
    tops = [g for g in lanes["groups"] if g["parent"] is None]
    assert [g["id"] for g in tops] == ["rec:r1", "rec:r2"] and all(g["kind"] == "record" for g in tops)
    assert all(g["parent"] in ("rec:r1", "rec:r2") for g in lanes["groups"] if g["kind"] == "paragraph")
    assert {c["span"]["path"] for c in lanes["cards"]} == {"r1", "r2"} and lanes["source"]["record_ids"] == ["r1", "r2"]
    assert _run(X.explode_prose(record_id="nope"))["error"] == "record nope not found"
    assert "required" in _run(X.explode_prose())["error"]


def test_the_layer_list_is_the_registry_in_order():
    rows = X.layer_list()
    assert [r["id"] for r in rows][:4] == ["ner", "ner.node", "rel.typed", "rel.cooccur"]
    assert all("fn" not in r for r in rows) and rows[0]["default_on"] and not {r["id"]: r for r in rows}["langid"]["default_on"]
