"""memory.seek records a timing envelope (four-minds FM-7.0, slice 2).

memory_retrieval imports the orchestrator, so _gather_ranked_lists and _leg are lifted out
with ast and run against a fake data fabric.
"""
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

pytestmark = pytest.mark.critical

SRC = os.path.join(ROOT, "vera", "fabric", "memory_retrieval.py")
TEXT = open(SRC, encoding="utf-8").read()
TREE = ast.parse(TEXT)


def _fn(name):
    return next(n for n in TREE.body
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)


def _gather():
    ns = {"asyncio": asyncio, "time": time, "log": logging.getLogger("t"), "_rtc": rtc,
          "_bounded_embed_result": qec.bounded_embed_result, "_SEEK_EMBED_WAIT_S": 0.2,
          "List": list, "Dict": dict}
    for name in ("_leg", "_gather_ranked_lists"):
        exec(compile(ast.get_source_segment(TEXT, _fn(name)), SRC, "exec"), ns)
    return ns["_gather_ranked_lists"]


def _df(embed_delay=0.0, chroma=None, pg=None, pg_exc=None):
    async def _embed(q):
        await asyncio.sleep(embed_delay)
        return [0.1, 0.2]

    async def search_text(q, ds, limit=10):
        if pg_exc:
            raise pg_exc
        return pg or []

    return types.SimpleNamespace(
        _embed=_embed,
        FAISS_STORE=types.SimpleNamespace(available=False),
        FABRIC_CHROMA=types.SimpleNamespace(search=lambda emb, dataset_id=None, top_k=10: chroma or []),
        FABRIC_PG=types.SimpleNamespace(available=True, search_text=search_text))


def _outcomes(env):
    return {s["stage"]: s["outcome"] for s in env.stages}


def test_embed_timeout_is_not_an_empty_vector_search():
    env = rtc.Envelope("memory.seek")
    ranked, sims, text_ids = asyncio.run(
        _gather()(_df(embed_delay=1.0, pg=[("r1", 0.5)]), "q", "", 10, env))
    assert _outcomes(env) == {"embed": "timeout", "chroma": "skipped:no-vector", "pg_text": "ok"}
    assert ranked == [["r1"]] and text_ids == {"r1"}


def test_each_leg_reports_its_own_outcome():
    env = rtc.Envelope("memory.seek")
    ranked, sims, _ = asyncio.run(
        _gather()(_df(chroma=[("c1", 0.7)], pg_exc=RuntimeError("pg down")), "q", "", 10, env))
    assert _outcomes(env) == {"embed": "ok", "chroma": "ok", "pg_text": "error"}
    assert ranked == [["c1"]] and sims == {"c1": 0.7}


def test_without_an_envelope_nothing_changes():
    ranked, _, _ = asyncio.run(_gather()(_df(chroma=[("c1", 0.7)], pg=[("p1", 1.0)]), "q", "", 10))
    assert ranked == [["c1"], ["p1"]]


def test_seek_records_on_every_return():
    seek = ast.get_source_segment(TEXT, _fn("_memory_seek_body"))     # memory.seek's body since D10-1
    assert seek.count("_record(env)") == 2
    for stage in ('"fuse"', '"hydrate"', '"memory"', '"select"', '"legacy"'):
        assert stage in seek
