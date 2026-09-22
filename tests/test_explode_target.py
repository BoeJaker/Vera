# -*- coding: utf-8 -*-
"""explode.target (EXPLODE-R2.md R5): what IS this thing, and how would it explode?

The reported failure: clicking `topic_memgraph_repo_helm_charts` on the graph answered "explode failed: record
topic_memgraph_repo_helm_charts not found". It was never a record -- checked live, it is a Dataset node holding
four FabricRecords. These checks pin the shapes the live graph actually holds (counted 2026-09-22: FabricRecord
608k, Memory 343k, Entity 267k, Dataset 5k, CodeFile 5k, CodeFunction 4k) and the rule that nothing explodable is
a sentence about the thing, never "not found" for something that was never a record.
"""
import asyncio

import pytest

from vera.research import explode_capabilities as X

REPO_FILE = "vera/research/assess_core.py"      # a file this checkout really has


def run(**kw):
    return asyncio.get_event_loop().run_until_complete(X.resolve_target(**kw))


@pytest.fixture(autouse=True)
def graph(monkeypatch):
    """The live shapes, as fakes: the record store, and the two Cypher reads the resolver makes."""
    records = {"rec1": {"id": "rec1", "dataset_id": "topic_x", "text": "Northwind will acquire Contoso.\nA second line."}}
    nodes = {
        "topic_memgraph_repo_helm_charts": {"props": {"id": "topic_memgraph_repo_helm_charts", "source": "repo",
                                                      "updated_at": "2026-06-02T18:49:01Z"},
                                            "ls": ["Dataset"], "parent_path": None, "parent_lang": None},
        "ent_bristol": {"props": {"id": "ent_bristol", "text": "Bristol", "type": "GPE", "confidence": 0.9},
                        "ls": ["Entity", "ExtractedEntity", "GPE"], "parent_path": None, "parent_lang": None},
        "mem_1": {"props": {"id": "mem_1", "human_text": True, "ai_output": True,
                            "summary": "The deal is expected to close in June, pending approval by the "
                                       "Competition and Markets Authority, and the board will meet first."},
                  "ls": ["Memory"], "parent_path": None, "parent_lang": None},
        "file_1": {"props": {"id": "file_1", "filepath": REPO_FILE, "language": "python"},
                   "ls": ["Entity", "File", "CodeFile"], "parent_path": None, "parent_lang": None},
        "file_gone": {"props": {"id": "file_gone", "filepath": "somewhere/else/gone.py"},
                      "ls": ["CodeFile"], "parent_path": None, "parent_lang": None},
        "func_1": {"props": {"id": "func_1", "function_name": "score_readability", "line_start": 10},
                   "ls": ["Entity", "CodeFunction", "Function", "Method"],
                   "parent_path": REPO_FILE, "parent_lang": "python"},
        "surface_1": {"props": {"id": "surface_1", "name": "vera.int", "url": "https://vera.int", "kind": "host"},
                      "ls": ["Surface"], "parent_path": None, "parent_lang": None},
        "resp_short": {"props": {"id": "resp_short", "text": "tool\nfocus code analysis", "topic": "", "type": "response"},
                       "ls": ["Entity", "Response"], "parent_path": None, "parent_lang": None},
        "ent_listed": {"props": {"id": "ent_listed", "record_ids": ["r9", "r8"]},
                       "ls": ["Entity"], "parent_path": None, "parent_lang": None},
        "rec_elsewhere": {"props": {"id": "rec_elsewhere", "dataset_id": "topic_helm",
                                    "title": "Memgraph configMap for configuration"},
                          "ls": ["FabricRecord"], "parent_path": None, "parent_lang": None},
    }
    contains = {"topic_memgraph_repo_helm_charts": [{"rid": "2989ed8c", "rel": "CONTAINS"},
                                                    {"rid": "8b81e3bc", "rel": "CONTAINS"},
                                                    {"rid": "219f8606", "rel": "CONTAINS"},
                                                    {"rid": "fbfac262", "rel": "CONTAINS"}],
                "ent_bristol": [{"rid": "r1", "rel": "MENTIONED_IN"}, {"rid": "r2", "rel": "MENTIONED_IN"}]}
    canvas = {"id": "cv_1", "blocks": [
        {"id": "b1", "key": "notes", "type": "markdown", "content": {"md": "A paragraph long enough to read." * 2}},
        {"id": "b2", "key": "impl", "type": "code", "content": {"code": "def f():\n    return 1\n", "lang": "python",
                                                                "filename": "f.py"}},
        {"id": "b3", "key": "pic", "type": "image", "content": {"url": "x"}}]}

    async def fake_rows(cypher, **params):
        if "CodeFile)-[]->(n)" in cypher:
            n = nodes.get(params.get("id"))
            return [dict(n)] if n else []
        if "FabricRecord)" in cypher:
            return list(contains.get(params.get("id"), []))[: params.get("k", 8)]
        return []

    async def fake_cap(name, **kw):
        if name == "canvas.get":
            return dict(canvas) if kw.get("id") == "cv_1" else {"error": "unknown canvas"}
        return None

    monkeypatch.setattr(X, "_read_record", lambda rid: records.get(rid))
    monkeypatch.setattr(X, "_aux_rows", fake_rows)
    monkeypatch.setattr(X, "_call_cap", fake_cap)
    return nodes


def test_the_reported_node_is_a_dataset_of_records_not_a_missing_record():
    d = run(id="topic_memgraph_repo_helm_charts")
    assert d["ok"] and d["what"] == "records"
    assert d["cap"] == "nlp.explode.prose"
    assert d["args"]["record_ids"] == ["2989ed8c", "8b81e3bc", "219f8606", "fbfac262"]
    assert "Dataset" in d["why"] and "contains" in d["why"].lower()
    assert "not found" not in d["why"]          # it was never a record: never say that about it


def test_a_fabric_record_is_itself_and_the_label_is_its_first_line():
    d = run(id="rec1")
    assert d["ok"] and d["what"] == "record" and d["args"] == {"record_id": "rec1"}
    assert "Northwind will acquire Contoso." in d["label"]


def test_an_entity_resolves_to_the_records_that_mention_it_the_evidence():
    d = run(id="ent_bristol")
    assert d["ok"] and d["what"] == "records" and d["args"]["record_ids"] == ["r1", "r2"]
    assert "mentioned in" in d["why"].lower()


def test_an_entity_that_names_its_records_in_a_property_uses_them():
    d = run(id="ent_listed")
    assert d["ok"] and d["args"]["record_ids"] == ["r9", "r8"]


def test_a_memory_explodes_its_own_text_and_an_untyped_property_is_not_trusted():
    d = run(id="mem_1")
    assert d["ok"] and d["what"] == "text"
    assert d["args"]["text"].startswith("The deal is expected")     # human_text is a BOOLEAN on this row
    assert "`summary`" in d["why"]


def test_a_code_file_explodes_as_code_and_a_function_explodes_its_file():
    f = run(id="file_1")
    assert f["ok"] and f["what"] == "code" and f["cap"] == "code.explode" and f["args"]["path"] == REPO_FILE
    fn = run(id="func_1")
    # a function clicked on the graph asks what it DOES, so it resolves to its flow, not to its file
    assert fn["ok"] and fn["what"] == "flow" and fn["args"]["path"] == REPO_FILE
    assert fn["args"]["flow"] == "score_readability"
    assert "score_readability" in fn["label"] and "source order" in fn["why"]


def test_a_file_the_graph_knows_but_this_checkout_does_not_says_so():
    d = run(id="file_gone")
    assert not d["ok"] and "not in this checkout" in d["why"] and "gone.py" in d["why"]


def test_a_node_with_neither_text_nor_records_is_told_what_it_is():
    d = run(id="surface_1")
    assert not d["ok"] and d["what"] == "nothing"
    assert "Surface" in d["why"] and "name" in d["why"] and "url" in d["why"]
    assert "not found" not in d["why"]
    assert d["seen"]["labels"] == ["Surface"]


def test_an_id_that_is_nothing_at_all_says_what_it_looked_for():
    d = run(id="no_such_thing")
    assert not d["ok"]
    for w in ("no fabric record", "no graph node", "no repo file"):
        assert w in d["why"]


def test_text_a_snippet_and_a_repo_path_resolve_without_a_lookup():
    assert run(text="A passage.")["cap"] == "nlp.explode.prose"
    snip = run(text="def f(): pass", lang="python")
    assert snip["what"] == "code" and snip["args"]["lang"] == "python"
    p = run(path=REPO_FILE)
    assert p["ok"] and p["what"] == "code" and p["args"]["path"] == REPO_FILE
    assert not run(path="no/such/file.py")["ok"]
    assert run(id=REPO_FILE)["what"] == "code"                     # a path given where an id goes still works


def test_a_canvas_item_resolves_to_its_code_or_its_prose_and_a_wrong_key_lists_the_right_ones():
    c = run(canvas_id="cv_1", key="impl")
    assert c["ok"] and c["what"] == "code" and c["args"]["lang"] == "python"
    t = run(canvas_id="cv_1", key="notes")
    assert t["ok"] and t["what"] == "text" and t["args"]["text"].startswith("A paragraph")
    i = run(canvas_id="cv_1", key="pic")
    assert not i["ok"] and "image" in i["why"]
    miss = run(canvas_id="cv_1", key="nope")
    assert not miss["ok"] and "notes" in miss["why"] and "impl" in miss["why"]


def test_nothing_at_all_asks_for_something_explodable():
    d = run()
    assert not d["ok"] and "repo path" in d["why"] and "text" in d["why"]



def test_a_record_the_graph_knows_but_this_instance_does_not_hold_is_still_a_record():
    """Live on the design mirror: its record store has 41k records, the shared graph 608k. A record node from the
    far side of that gap used to come back as "nothing to explode", which is false twice over -- it IS a record,
    and it IS explodable, just not here."""
    d = run(id="rec_elsewhere")
    assert d["what"] == "record" and d["args"] == {"record_id": "rec_elsewhere"}
    assert not d["ok"]                                   # not HERE: this instance has no text for it
    assert "per instance" in d["why"] and "topic_helm" in d["why"]
    assert "not found" not in d["why"]
    assert "Memgraph configMap" in d["label"]



def test_the_answer_reads_as_english_because_the_sentence_IS_the_feature():
    """Live on the mirror the answers read "a Entity node" for a Response, and "the 5 records it mentioned in".
    The whole point of the resolver is the sentence a reader gets instead of a false error."""
    ent = run(id="ent_bristol")
    # labels ["Entity", "ExtractedEntity", "GPE"]: the last one is the one worth saying
    assert "a GPE node" in ent["why"] and "the 2 records it is mentioned in" in ent["why"]
    assert "an Entity node" in run(id="ent_listed")["why"]          # and the article follows the word
    ds = run(id="topic_memgraph_repo_helm_charts")
    assert "a Dataset node" in ds["why"] and "the 4 records it contains" in ds["why"]
    # Neo4j gives labels general-first, so the LAST one is the informative one
    short = run(id="resp_short")
    assert not short["ok"] and short["label"].startswith("Response ")
    assert "a Response node" in short["why"]
    assert "too short to have structure" in short["why"]        # not "no text": it HAS text, just not enough
