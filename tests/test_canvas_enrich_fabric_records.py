"""The fabric's answer as canvas records (enrich_core.records_from / records_item).

2026-09-28 (owner): "the fabric one just seems to return one line results - itd be better if it displayed a vera graph
.js graph by default but the list can be an option". The rows below are the shape fabric.query answered with on prod
(2026-09-29, "vector database comparison"): no title, the text alone, `score` its rank fusion and `vector_score` its
similarity, tags as a list.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from vera.canvas import enrich_core as ec  # noqa: E402

ANSWER = {"results": [
    {"id": "k_4aae", "score": 0.015873, "vector_score": 0.6525, "text_score": 0.0, "dataset_id": "board_index",
     "text": "W5-04 retrieval comparison\nDeterministic retrieval comparison foundation complete. Exact immutable "
             "DatasetSnapshot and identical case/citation fixtures.",
     "created_at": "2026-09-27 13:32:24+00:00", "tags": ["board", "index"], "source": "board.index"},
    {"id": "b47f", "score": 0.016393, "vector_score": 0.6607, "text_score": 0.0, "dataset_id": "topic.memgraph",
     "text": "Vector Search in Memgraph Similarity and structure in a single engine. Run vector search and graph "
             "traversal together.", "tags": ["topic", "web", "discovery"], "source": "fabric_discovery"},
], "count": 2}


def test_a_titleless_record_is_named_by_its_first_line_and_its_body_is_the_rest():
    recs = ec.records_from("fabric.query", ANSWER, kind="memory")
    r = recs[0]
    assert r["title"] == "W5-04 retrieval comparison"
    assert r["snippet"].startswith("Deterministic retrieval comparison")      # not the title again
    assert recs[1]["title"] == "Vector Search in Memgraph Similarity and structure in a single engine"


def test_relevance_is_the_similarity_not_the_rank_fusion():
    recs = ec.records_from("fabric.query", ANSWER, kind="memory")
    assert recs[0]["score"] == 0.652 and recs[1]["score"] == 0.661
    assert "vector_score" not in recs[0].get("meta", {}) and "text_score" not in recs[0].get("meta", {})


def test_tags_ride_along_for_the_graph():
    recs = ec.records_from("fabric.query", ANSWER, kind="memory")
    assert recs[0]["tags"] == ["board", "index"] and recs[1]["tags"] == ["topic", "web", "discovery"]
    assert recs[0]["meta"]["dataset_id"] == "board_index"


def test_the_fabric_item_opens_as_a_graph_and_the_others_as_lists():
    mem = ec.records_item("fabric.query", {"text": "vector"}, ANSWER, kind="memory")
    assert mem["view"] == "graph"
    web = ec.records_item("web.search", {"query": "x"}, {"results": [{"url": "https://a.org", "title": "A"},
                                                                     {"url": "https://b.org", "title": "B"}]}, kind="web")
    assert "view" not in web


def test_a_web_result_keeps_its_own_score():
    recs = ec.records_from("web.search", {"results": [{"url": "https://a.org", "title": "A", "score": 0.4}]}, kind="web")
    assert recs[0]["score"] == 0.4
