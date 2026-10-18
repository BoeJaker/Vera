"""fusion_core (four-minds FM-7.1) and memory.seek's use of it (RF-04)."""
import ast
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from vera.fabric import fusion_core as fc  # noqa: E402

pytestmark = pytest.mark.critical


def test_rrf_sums_ranks_and_ignores_repeats():
    s = fc.rrf([["a", "b"], ["b", "a", "a"]], k=60)
    assert s["a"] == pytest.approx(1 / 60 + 1 / 61)
    assert s["b"] == pytest.approx(1 / 61 + 1 / 60)


def test_rrf_weights():
    s = fc.rrf([["m"]], weights=[3.0], k=60)
    assert s["m"] == pytest.approx(3 / 60)
    with pytest.raises(ValueError):
        fc.rrf([["a"], ["b"]], weights=[1.0])


def test_list_weight_puts_one_list_on_the_scale_of_n():
    fabric = fc.rrf([["r"], ["r"], ["r"]])           # top of all three fabric lists
    memory = fc.rrf([["m"]], weights=[fc.list_weight(3)])
    assert memory["m"] == pytest.approx(fabric["r"])
    assert fc.list_weight(0) == 1.0


def test_query_terms_drop_stopwords_and_short_tokens():
    assert fc.query_terms("What is the Redis license change?") == {"redis", "license", "change"}
    assert fc.query_terms("tell me about it") == set()


def test_lexical_pass():
    terms = fc.query_terms("redis license change")
    assert fc.lexical_pass(terms, "Redis moved to the SSPL license in 2024")
    assert not fc.lexical_pass(terms, "the weather in London")
    assert not fc.lexical_pass(set(), "anything")
    assert fc.lexical_pass(terms, "Redis license", min_overlap=2)


def test_seek_uses_the_core_for_memory_hits():
    src = open(os.path.join(ROOT, "vera", "fabric", "memory_retrieval.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    funcs = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)}
    mem = ast.dump(funcs["_memory_candidates"])
    assert "lexical_pass" in mem and "weight" in mem
    seek = ast.get_source_segment(src, funcs["_memory_seek_body"])    # memory.seek's body since D10-1
    assert "_fc.rrf(" in seek and "list_weight(len(ranked))" in seek


def test_lexical_query_keeps_short_queries_as_they_are():
    assert fc.lexical_query("telegram bot") == "telegram bot"
    assert fc.lexical_query("what is the redis port") == "what is the redis port"


def test_lexical_query_keeps_content_terms_of_a_long_question():
    q = "what did we set up for telegram, the bot token and the bot thing, is it working"
    assert fc.lexical_query(q) == "set telegram bot token thing working"


def test_lexical_query_of_stopwords_only_goes_as_it_is():
    q = "what is it that you would have been about to do there"
    assert fc.lexical_query(q) == q


def test_neo4j_leg_sends_the_lexical_query():
    src = open(os.path.join(ROOT, "vera", "fabric", "memory.py"), encoding="utf-8").read()
    assert 'params["query"] = _lucene_escape(_fusion_core.lexical_query(query))' in src
