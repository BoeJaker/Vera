"""Chroma calls are bounded and run on their own pool (RF-77: 4 Oct 2026, all 16
default-executor threads parked in chromadb calls with no timeout)."""

import asyncio
import importlib.util
import pathlib
import threading

import httpx

ROOT = pathlib.Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("cbc", ROOT / "vera" / "fabric" / "chroma_bound_core.py")
C = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(C)
MEM = (ROOT / "vera" / "fabric" / "memory.py").read_text(encoding="utf-8")
FAB = (ROOT / "vera" / "fabric" / "data_fabric.py").read_text(encoding="utf-8")
ORCH = (ROOT / "vera" / "capability_orchestration.py").read_text(encoding="utf-8")


class _Server:
    def __init__(self):
        self._session = httpx.Client(timeout=None)


class _Client:
    def __init__(self):
        self._server = _Server()


def test_bound_sets_a_finite_timeout():
    c = _Client()
    assert C.bound(c, read_s=42, connect_s=5) is True
    t = c._server._session.timeout
    assert t.read == 42 and t.connect == 5


def test_bound_default_is_finite():
    c = _Client()
    assert C.bound(c) is True
    assert c._server._session.timeout.read == C.READ_S > 0


def test_bound_never_raises_on_odd_clients():
    assert C.bound(object()) is False
    assert C.bound(None) is False


def test_run_uses_the_chroma_pool():
    names = asyncio.run(C.run(lambda: threading.current_thread().name))
    assert names.startswith("vera-chroma")


def test_run_passes_args_and_kwargs():
    assert asyncio.run(C.run(lambda a, b=0: a + b, 2, b=3)) == 5


def test_memory_chroma_backend_uses_the_pool_and_bound():
    cls = MEM[MEM.index("class ChromaBackend("):MEM.index("class Neo4jBackend(")]
    assert "asyncio.to_thread(" not in cls
    assert cls.count("_chroma_bound.run(") >= 7
    assert "_chroma_bound.bound(client)" in cls


def test_fabric_store_is_bound_and_upserts_use_the_pool():
    assert "_chroma_bound.bound(self._client)" in FAB
    assert "run_in_executor(None, FABRIC_CHROMA.upsert" not in FAB
    assert FAB.count("run_in_executor(_chroma_bound.pool(), FABRIC_CHROMA.upsert") == 2


def test_orchestration_client_is_bound():
    assert "_chroma_bound_core.bound(CHROMA)" in ORCH
