# -*- coding: utf-8 -*-
"""rel.model (EXPLODE.md §4, S7): a model types the relation, instead of a cue.

`rel.typed` reads relations from the words between two entities and `rel.cooccur` says only that two names shared
a sentence. Neither knows what the sentence MEANS. The node tier already serves an NLI model, and relation
extraction is what NLI does when the hypothesis is a relation. The null label is the point of the design: most
sentences that name two things state no relation between them, and a forced choice would invent one.
"""
import asyncio

import pytest

from vera.research import explode_capabilities as X

TEXT = ("Northwind acquired Contoso for 40 million pounds. Alice Carter and Bob Ng attended. "
        "Carter leads Northwind.")


def card(cid, title, kind, start):
    return {"id": cid, "layer": "ner", "kind": kind, "title": title,
            "span": {"path": "", "start": start, "end": start + len(title)}, "fields": [], "badges": []}


def ctx_of(cards):
    return {"text": TEXT, "base": 0, "path": "", "content_type": "text",
            "paragraphs": X.paragraphs_of(TEXT, 0), "sentences": X.sentences_of(TEXT, 0),
            "cards": list(cards), "edges": [], "assessments": [], "ent_list": []}


# Northwind is named again in the third sentence: coref records that as a mention of the same card, and a
# sentence that names an entity again is a sentence about it
NORTHWIND = card("n", "Northwind", "org", TEXT.index("Northwind"))
NORTHWIND["mentions"] = [{"start": TEXT.rindex("Northwind"), "end": TEXT.rindex("Northwind") + 9, "by": "exact"}]

CARDS = [NORTHWIND,
         card("c", "Contoso", "org", TEXT.index("Contoso")),
         card("a", "Alice Carter", "person", TEXT.index("Alice Carter")),
         card("b", "Bob Ng", "person", TEXT.index("Bob Ng")),
         card("k", "Carter", "person", TEXT.index("Carter leads"))]


def fake(answers, calls):
    """A stand-in for the node tier: each sentence gets the label the test chose for it."""
    async def _call(name, **kw):
        assert name == "nlp.zeroshot"
        calls.append(kw)
        lab, score = answers[len(calls) - 1]
        return {"labels": [{"label": lab, "score": score}], "model": "deberta-mnli", "node": "cpu-246"}
    return _call


def run(ctx):
    return asyncio.get_event_loop().run_until_complete(X._layer_rel_model(ctx))


def test_the_sentence_is_the_premise_and_the_relation_is_typed_with_its_score(monkeypatch):
    calls = []
    monkeypatch.setattr(X, "_call_cap", fake([(X._REL_LABELS[0], 0.91), (X._REL_NULL, 0.7), (X._REL_LABELS[1], 0.52)], calls))
    out = run(ctx_of(CARDS))
    assert [c["text"][:20] for c in calls][0].startswith("Northwind acquired")
    assert len(calls) == 3                                   # one per sentence that names two entities
    assert all(set(c["labels"]) == set(X._REL_LABELS) for c in calls)
    assert calls[2]["text"].startswith("Carter leads")        # a later mention is still that entity being named
    e = out["edges"][0]
    assert e["from"] == "n" and e["to"] == "c" and e["label"] == "acquired"
    assert e["kind"] == "RELATES" and e["layer"] == "rel.model" and e["score"] == 0.91
    assert e["resolution"] == "exact" and "deberta-mnli" in e["by"]
    assert e["span"]["start"] == 0                           # the sentence it was read from
    assert out["where"] == "cpu-246"


def test_a_sentence_that_states_no_relation_says_so_rather_than_inventing_one(monkeypatch):
    calls = []
    monkeypatch.setattr(X, "_call_cap", fake([(X._REL_NULL, 0.88)] * 3, calls))
    out = run(ctx_of(CARDS))
    assert out["edges"] == [] and out["count"] == 0
    assert "3 sentences" in out["note"] and "named together" in out["note"]


def test_a_weak_answer_is_not_taken_and_a_middling_one_says_it_is_heuristic(monkeypatch):
    calls = []
    monkeypatch.setattr(X, "_call_cap", fake([(X._REL_LABELS[0], 0.30), (X._REL_LABELS[1], 0.50), (X._REL_LABELS[1], 0.50)], calls))
    out = run(ctx_of(CARDS))
    assert len(out["edges"]) == 2 and all(e["resolution"] == "heuristic" for e in out["edges"])


def test_it_asks_only_about_sentences_that_name_two_things_and_caps_what_it_asks(monkeypatch):
    calls = []
    one = [card("n", "Northwind", "org", TEXT.index("Northwind"))]
    monkeypatch.setattr(X, "_call_cap", fake([], calls))
    assert run(ctx_of(one))["count"] == 0 and not calls      # one entity is not a relation
    long_text = " ".join("Alpha met Beta." for _ in range(40))
    cards = []
    for i, m in enumerate(__import__("re").finditer(r"Alpha|Beta", long_text)):
        cards.append(card("c%d" % i, m.group(0), "org", m.start()))
    ctx = {"text": long_text, "base": 0, "path": "", "content_type": "text",
           "paragraphs": X.paragraphs_of(long_text, 0), "sentences": X.sentences_of(long_text, 0),
           "cards": cards, "edges": [], "assessments": [], "ent_list": []}
    calls2 = []
    monkeypatch.setattr(X, "_call_cap", fake([(X._REL_NULL, 0.9)] * 100, calls2))
    run(ctx)
    assert len(calls2) == X._REL_MAX_SENTENCES              # a node tier is not held for minutes by one diagram


def test_the_node_being_unavailable_is_a_receipt_not_a_broken_diagram(monkeypatch):
    async def down(name, **kw):
        return {"error": "no node has zeroshot"}
    monkeypatch.setattr(X, "_call_cap", down)
    out = run(ctx_of(CARDS))
    assert out["error"] == "no node has zeroshot" and out["count"] == 0


def test_the_layer_is_off_by_default_and_says_what_it_costs():
    row = next(l for l in X.layer_list() if l["id"] == "rel.model")
    assert row["default_on"] is False and row["needs"] == ["ner"] and row["where"] == "node tier"
    assert "one call per sentence" in row["note"] and "no relation stated" in row["note"]
    ids = [l["id"] for l in X.layer_list()]
    assert ids.index("rel.model") > ids.index("coref")      # it types relations between the MERGED entities
