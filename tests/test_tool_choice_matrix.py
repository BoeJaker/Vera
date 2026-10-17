"""Tool choice in the candidate matrix (owner, 17 Oct: every application x every candidate): several rankers on
the same held-out loop steps - kNN, the usage prior, the lexical scorer, kNN reordered by the cross-encoder."""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from vera.minds import tool_shortlist_core as TC  # noqa: E402

pytestmark = pytest.mark.critical


def _rows():
    web, code = [1.0, 0.0], [0.0, 1.0]
    return [
        {"run": "r1", "vec": web, "tools": ["web.search"], "title": "search the web for the latest rate"},
        {"run": "r2", "vec": web, "tools": ["web.search"], "title": "search news about the bank"},
        {"run": "r3", "vec": code, "tools": ["code.author"], "title": "write the python script"},
        {"run": "r4", "vec": code, "tools": ["code.author"], "title": "author a python module"},
        {"run": "r5", "vec": web, "tools": ["web.search", "memory.seek"], "title": "search and recall the notes"},
    ]


CATALOGUE = {
    "web.search": {"tags": ["web"], "description": "Search the web for pages about a query."},
    "code.author": {"tags": ["code"], "description": "Write python code files for a task."},
    "memory.seek": {"tags": ["memory"], "description": "Recall notes and past records from memory."},
    "image.generate": {"tags": ["image"], "description": "Draw a picture from a prompt."},
}


def test_replay_is_unchanged_and_is_the_knn_row_of_the_matrix():
    rows = _rows()
    m = TC.replay_matrix(rows, CATALOGUE)
    assert TC.replay(rows) == m["knn:steps+prior"]
    assert m["knn:steps+prior"]["hit@5"] == 1.0


def test_every_ranker_is_scored_on_the_same_steps():
    rows = _rows()
    m = TC.replay_matrix(rows, CATALOGUE, reranked={0: ["web.search"], 2: ["memory.seek", "code.author"]})
    assert set(m) == {"knn:steps+prior", "prior", "lexical", "knn+rerank:msmarco-minilm"}
    assert m["prior"]["n"] == 5 and m["lexical"]["n"] == 5
    assert m["knn+rerank:msmarco-minilm"]["n"] == 2                 # only the re-ranked sample counts
    assert m["knn+rerank:msmarco-minilm"]["mrr"] == pytest.approx((1.0 + 0.5) / 2, abs=1e-3)
    assert "lexical" not in TC.replay_matrix(rows)                  # no catalogue, no lexical row


def test_the_lexical_ranker_reads_the_step_words():
    assert TC.lexical_ranking("write the python script", CATALOGUE, 3)[0] == "code.author"
    assert TC.lexical_ranking("search the web", CATALOGUE, 3)[0] == "web.search"
    assert TC.lexical_ranking("", CATALOGUE, 3) == []


def test_the_prior_never_learns_from_the_steps_own_run():
    rows = [{"run": "a", "vec": [1.0], "tools": ["only.mine"], "title": "x"},
            {"run": "b", "vec": [1.0], "tools": ["common"], "title": "y"},
            {"run": "c", "vec": [1.0], "tools": ["common"], "title": "z"}]
    m = TC.replay_matrix(rows)
    assert m["prior"]["unknown_rate"] > 0 and m["prior"]["recall@5"] == pytest.approx(2 / 3, abs=1e-3)


def test_the_nightly_replay_keeps_the_matrix():
    src = open(os.path.join(ROOT, "vera", "minds", "tool_shortlist.py"), encoding="utf-8").read()
    assert '"matrix": matrix' in src and "await _rerank_sample(rows, catalogue)" in src
    assert "SELECT run, vec, tools, title FROM tool_step_index" in src
