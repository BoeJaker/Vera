"""memory.seek's keyword leg on a Postgres full-text index (four-minds FM-7.1): the SQL matches the index expression,
all-word records come first, and the leg falls back to the ILIKE scan whenever the index cannot answer; behind
memory.text_index, 'like' by default."""
import ast
import asyncio
import logging
import os
import sys
import time
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from vera.dag import query_embed_core as qec  # noqa: E402
from vera.fabric import retrieval_timing_core as rtc  # noqa: E402
from vera.fabric import text_index_core as T  # noqa: E402

pytestmark = pytest.mark.critical

SRC = os.path.join(ROOT, "vera", "fabric", "memory_retrieval.py")
TEXT = open(SRC, encoding="utf-8").read()
TREE = ast.parse(TEXT)


def test_terms_are_plain_words_so_nothing_can_be_injected():
    assert T.terms("How do I restart the Ollama wedge? restart!") == ["how", "restart", "the", "ollama", "wedge"]
    assert T.terms("a & b | !c:* ") == []
    assert T.terms("x" * 3 + " " + " ".join(f"w{i:02d}" for i in range(30)))[-1] == "w10"     # MAX_TERMS
    assert T.tsquery(["a1", "b2"], "&") == "a1 & b2" and T.tsquery([], "|") == ""


def test_the_sql_uses_the_index_expression_and_binds_every_value():
    assert T.VECTOR in T.CREATE and "CONCURRENTLY" in T.CREATE
    for tier in ("all", "any"):
        for ds in (False, True):
            sql = T.search_sql(tier, ds)
            assert sql.count(T.VECTOR) >= 2 and "$1" in sql and "$2" in sql and ("$3" in sql) == ds
            assert "ILIKE" not in sql
    assert f"LIMIT {T.ANY_CAP})" in T.search_sql("any", False)


def test_all_word_records_come_first_and_each_tier_keeps_its_order():
    out = T.merge([("a", 0.4), ("b", 0.2)], [("c", 9.0), ("a", 5.0), ("d", 3.0)], limit=4)   # SQL order: rank desc
    assert [i for i, _ in out] == ["a", "b", "c", "d"]
    scores = dict(out)
    assert scores["a"] > scores["b"] >= 0.5 > scores["c"] > scores["d"]
    assert T.merge([("z", 0.0)], [("c", 1.0)], 5) == [("z", 0.5), ("c", 0.45)] and T.merge([("a", 0)], [], 0) == []


def _fn(name):
    return next(n for n in TREE.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)


def _gather(mode):
    async def _switch(fid):
        return mode if fid == "memory.text_index" else "off"

    ns = {"asyncio": asyncio, "time": time, "log": logging.getLogger("t"), "_rtc": rtc, "_switch": _switch,
          "_bounded_embed_result": qec.bounded_embed_result, "_SEEK_EMBED_WAIT_S": 0.2, "List": list, "Dict": dict}
    for name in ("_leg", "_gather_ranked_lists"):
        exec(compile(ast.get_source_segment(TEXT, _fn(name)), SRC, "exec"), ns)
    return ns["_gather_ranked_lists"]


def _df(fts):
    async def _embed(q):
        return [0.1, 0.2]

    async def search_text(q, ds, limit=10):
        return [("like1", 1.0)]

    async def search_text_fts(q, ds, limit=10):
        if isinstance(fts, BaseException):
            raise fts
        return fts

    return types.SimpleNamespace(
        _embed=_embed, FAISS_STORE=types.SimpleNamespace(available=False),
        FABRIC_CHROMA=types.SimpleNamespace(search=lambda emb, dataset_id=None, top_k=10: []),
        FABRIC_PG=types.SimpleNamespace(available=True, search_text=search_text, search_text_fts=search_text_fts))


def _run(mode, fts):
    env = rtc.Envelope("memory.seek")
    ranked, _, text_ids = asyncio.run(_gather(mode)(_df(fts), "q", "", 10, env))
    return ranked, text_ids, [(s["stage"], s["outcome"]) for s in env.stages if s["stage"].startswith("pg_")]


def test_the_leg_uses_the_index_only_when_switched_on():
    assert _run("like", [("fts1", 1.0)])[0] == [["like1"]]
    ranked, ids, legs = _run("fts", [("fts1", 1.0), ("fts2", 0.4)])
    assert ranked == [["fts1", "fts2"]] and ids == {"fts1", "fts2"} and legs == [("pg_fts", "ok")]


def test_the_leg_falls_back_to_the_scan_when_the_index_cannot_answer():
    ranked, _, legs = _run("fts", None)                      # index missing or invalid
    assert ranked == [["like1"]] and legs == [("pg_text", "ok")]
    ranked, _, legs = _run("fts", RuntimeError("pg"))        # the indexed query failed: recorded, then the scan
    assert ranked == [["like1"]] and legs == [("pg_fts", "error"), ("pg_text", "ok")]


def test_the_switch_is_registered_like_by_default():
    from vera.minds import switchboard_core as SB
    f = SB.REGISTRY["memory.text_index"]
    assert f.modes == ("like", "fts") and f.default == "like" and f.package == "FM-7.1"


def test_the_index_is_built_only_by_prods_host_and_never_beside_the_dump():
    # backbone review 11 Oct: sandboxes and node workers share the database and must not build on it; no DDL in the
    # 01:00-03:30 dump window
    assert [h for h in range(24) if T.in_dump_window(h)] == [1, 2, 3]
    src = open(os.path.join(ROOT, "vera", "fabric", "data_fabric.py"), encoding="utf-8").read()
    assert "if FABRIC_PG.available and _tix_build_allowed():" in src
    guard = src[src.index("def _tix_build_allowed("):src.index('schedule(_startup, interval=999999, name="fabric_startup")')]
    assert "is_dev_sandbox()" in guard and "_IS_WORKER" in guard and "return False" in guard
    ens = src[src.index("    async def ensure_text_index("):src.index("    async def search_text_fts(")]
    assert ens.index("in_dump_window") < ens.index("_tix.CREATE") and "lock_timeout" in ens
