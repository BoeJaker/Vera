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
        time.sleep(3)
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
        t.join(15)
    assert h.status_code == 200 and answered_during, "/health waited for the inference to finish"
    body = h.json()
    assert body["workers"] == ns.NLP_WORKERS and body["inflight"] == 1
    assert out["r"].json() == {"label": "ok"}


def test_every_model_call_goes_through_the_pool():
    src = open(os.path.join(_ROOT, "edge", "nlp_server.py"), encoding="utf-8").read()
    assert "_guard(run_" not in src
    for fn in ("run_ner", "run_classify", "run_zeroshot", "run_qa", "run_langid", "run_embed", "run_rerank"):
        assert f"await _run({fn}," in src, fn
    assert 'NLP_WORKERS = max(1, int(os.getenv("VERA_NLP_WORKERS", "1") or 1))' in src
