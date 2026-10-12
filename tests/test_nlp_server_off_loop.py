"""The NLP server runs inference off its event loop (recon RF-03, four-minds pipeline 10).

Every handler used to run its model synchronously inside `async def`, so one long inference
blocked the whole server: other requests and /health waited, and the router read a busy node as
a dead one. Inference now runs in a pool of VERA_NLP_WORKERS (default 1, which keeps the CPU cap).
"""
import os
import sys
import threading
import time

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "edge"))
sys.path.insert(0, os.path.join(_ROOT, "vera", "research"))

pytestmark = pytest.mark.critical

pytest.importorskip("fastapi", reason="edge servers need fastapi")
from fastapi.testclient import TestClient      # noqa: E402


def test_health_answers_while_a_model_is_working(monkeypatch):
    ns = pytest.importorskip("nlp_server")
    started, finished = threading.Event(), threading.Event()

    def slow_classify(text, task):
        started.set()
        # 8 s, not 3: /health walks the model inventory, and on a loaded gate host (a census or chat run
        # beside it) that alone took over 3 s - the order check flaked 1 in 3 (12 Oct 2026).
        time.sleep(8)
        finished.set()
        return {"label": "ok"}
    monkeypatch.setattr(ns, "run_classify", slow_classify)
    with TestClient(ns.build_app()) as c:
        out = {}
        t = threading.Thread(target=lambda: out.setdefault("r", c.post("/classify", json={"text": "hi"})))
        t.start()
        assert started.wait(10), "the inference never started"
        h = c.get("/health")
        answered_during = not finished.is_set()     # no wall-clock threshold: only the order matters
        t.join(25)
    assert h.status_code == 200 and answered_during, "/health waited for the inference to finish"
    body = h.json()
    assert body["workers"] == ns.NLP_WORKERS and body["inflight"] == 1
    assert out["r"].json() == {"label": "ok"}


def test_every_model_call_goes_through_the_pool():
    src = open(os.path.join(_ROOT, "edge", "nlp_server.py"), encoding="utf-8").read()
    assert "_guard(run_" not in src
    for fn in ("run_ner", "run_classify", "run_zeroshot", "run_qa", "run_rerank"):
        assert f"await _run({fn}," in src, fn
    assert 'NLP_WORKERS = max(1, int(os.getenv("VERA_NLP_WORKERS", "1") or 1))' in src


def test_an_embed_does_not_wait_behind_a_rerank(monkeypatch):
    """The fast lane: with one shared worker busy on a slow rerank, an embed still answers."""
    ns = pytest.importorskip("nlp_server")
    if not ns.NLP_FAST_LANE:
        pytest.skip("fast lane switched off")
    started, finished = threading.Event(), threading.Event()

    def slow_rerank(query, documents, top_k):
        started.set()
        time.sleep(3)
        finished.set()
        return {"ranked": []}
    monkeypatch.setattr(ns, "run_rerank", slow_rerank)
    monkeypatch.setattr(ns, "run_embed", lambda texts: {"embeddings": [[0.1, 0.2]]})
    with TestClient(ns.build_app()) as c:
        t = threading.Thread(target=lambda: c.post("/rerank", json={"query": "q", "documents": ["a"]}))
        t.start()
        assert started.wait(10), "the rerank never started"
        e = c.post("/embed", json={"texts": ["hi"]})
        answered_during = not finished.is_set()
        h = c.get("/health").json()
        t.join(15)
    assert e.json() == {"embeddings": [[0.1, 0.2]]} and answered_during, "the embed waited for the rerank"
    assert h["fast_lane"] == ["embed", "langid"]


def test_embed_and_langid_take_the_fast_lane():
    src = open(os.path.join(_ROOT, "edge", "nlp_server.py"), encoding="utf-8").read()
    assert "await _run(run_embed, req.texts, fast=True)" in src
    assert "await _run(run_langid, req.text, fast=True)" in src


def test_build_app_imports_nothing():
    """serve() starts the preload thread before build_app(); an import inside build_app() can deadlock
    against the thread's imports (2026-10-09: the unit logged 'serving' and never bound its port)."""
    import ast
    src = open(os.path.join(_ROOT, "edge", "nlp_server.py"), encoding="utf-8").read()
    fn = next(n for n in ast.parse(src).body if isinstance(n, ast.FunctionDef) and n.name == "build_app")
    nested = [n for n in ast.walk(fn) if isinstance(n, (ast.Import, ast.ImportFrom))]
    names = sorted({a.name for n in nested for a in n.names})
    assert not [x for x in names if x in ("asyncio", "ThreadPoolExecutor", "concurrent.futures")], names


def test_the_app_is_built_before_the_preload_thread_starts():
    src = open(os.path.join(_ROOT, "edge", "nlp_server.py"), encoding="utf-8").read()
    main = src[src.index("def _main("):]
    assert main.index("app = build_app()") < main.index('threading.Thread(target=preload, name="nlp-preload"')
    assert "uvicorn.run(app, host=args.host, port=args.port)" in main
