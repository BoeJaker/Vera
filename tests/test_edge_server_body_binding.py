"""An edge server's POST endpoints must accept a body.

`edge/onnx_runtime.py` declared `POST /run/{slug}` taking a pydantic model and
could never accept a tensor. Every request returned

    422 {"loc": ["query", "req"], "msg": "Field required"}

no matter what was sent. The cause is not obvious and is easy to reintroduce:
the file had `from __future__ import annotations`, which stringifies every
annotation; FastAPI resolves a stringified annotation against the MODULE
globals; and the request model was declared INSIDE `build_app()`, where module
globals cannot see it. FastAPI does not raise on that — it quietly decides the
parameter must be a query parameter instead.

So the endpoint 422s while the source looks completely correct, which is why it
survived in a component described as deployable. Found 2026-09-20 against
fastapi 0.116.1 / pydantic 2.11.7.

This guards the BEHAVIOUR (a body reaches the handler), not the absence of the
import — a future rework may legitimately re-add it by moving the models to
module scope, and that must stay allowed.
"""
import os
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "edge"))
# The NLP server imports its chunking core by flat name, the way it is deployed.
sys.path.insert(0, os.path.join(_ROOT, "vera", "research"))

pytestmark = pytest.mark.critical

fastapi = pytest.importorskip("fastapi", reason="edge servers need fastapi")
from fastapi.testclient import TestClient      # noqa: E402


def _client(module_name):
    mod = pytest.importorskip(module_name)
    return TestClient(mod.build_app())


def test_nlp_server_post_reaches_the_handler():
    """A body must bind. The handler's own validation error proves it did —
    a 422 naming `query` would mean FastAPI never looked at the body."""
    c = _client("nlp_server")
    for path, body in (("/ner", {"text": ""}),
                       ("/classify", {"text": ""}),
                       ("/rerank", {"query": "", "documents": ["a"]})):
        r = c.post(path, json=body)
        assert r.status_code == 200, f"{path} did not bind its body: {r.text}"
        # The handler ran and rejected the input itself.
        assert "error" in r.json(), f"{path} returned {r.json()}"


def test_nlp_server_ner_accepts_a_real_body():
    c = _client("nlp_server")
    r = c.post("/ner", json={"text": "Alice", "max_chars": 500})
    assert r.status_code == 200
    # Either it ran, or the model runtime is absent on this box — both mean the
    # body arrived. What must NOT happen is a binding failure.
    assert "query" not in r.text or "Field required" not in r.text


def test_onnx_runtime_run_reaches_the_handler():
    pytest.importorskip("numpy", reason="onnx_runtime imports numpy at module scope")
    c = _client("onnx_runtime")
    r = c.post("/run/definitely-not-a-model", json={"X": [[1.0, 2.0]]})
    # 404 (no such artifact) is the handler talking. 422 would be the bug.
    assert r.status_code != 422, f"body was not bound: {r.text}"


def test_thread_cap_clamps_an_oversized_environment():
    """The cap is the setting that decides whether NLP on a shared node starves
    the ollama runner beside it. An environment that already exported a bigger
    value must be clamped DOWN, not deferred to."""
    nlp_server = pytest.importorskip("nlp_server")
    for var in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS"):
        assert int(os.environ[var]) <= nlp_server.NLP_THREADS, (
            f"{var}={os.environ[var]} exceeds the cap {nlp_server.NLP_THREADS}")
