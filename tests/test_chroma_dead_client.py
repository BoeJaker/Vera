"""Fabric Chroma errors are visible and a dead client reconnects (four-minds FM-7.1, found 11 Oct 2026: memory.seek's
semantic leg returned nothing in ~10 ms while a direct Chroma query answered - the error was logged at debug only)."""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from vera.fabric import chroma_bound_core as CB  # noqa: E402

pytestmark = pytest.mark.critical


def test_a_dead_client_is_told_apart_from_a_refused_query():
    assert CB.looks_dead(RuntimeError("Cannot send a request, as the client has been closed."))
    assert CB.looks_dead(ConnectionError("Connection refused"))
    assert CB.looks_dead(type("ReadTimeout", (Exception,), {})("timed out"))
    assert not CB.looks_dead(ValueError("Collection expecting embedding with dimension of 768, got 384"))
    assert not CB.looks_dead(ValueError("Expected where to have exactly one operator"))


def test_the_throttle_lets_one_warning_through_per_window():
    t = [0.0]
    th = CB.Throttle(300.0, clock=lambda: t[0])
    assert th.due("search") and not th.due("search") and th.due("upsert")
    t[0] = 301.0
    assert th.due("search")


def test_search_retries_only_a_dead_client_and_every_failure_counts():
    src = open(os.path.join(ROOT, "vera", "fabric", "data_fabric.py"), encoding="utf-8").read()
    store = src[src.index("class FabricChromaStore:"):src.index("FABRIC_CHROMA = FabricChromaStore()")]
    search = store[store.index("    def search(self, embedding"):store.index("    def delete_dataset(")]
    assert "if not _chroma_bound.looks_dead(e):" in search and "if not self.connect():" in search
    assert 'self._failed("search", e); return []' in search and "log.debug" not in search
    assert 'self._failed("upsert", e); return False' in store
    assert '"errors": dict(self.errors)' in store


def test_a_store_that_never_connected_retries_once_a_minute():
    # 12 Oct 2026: Chroma was slow during a restart, the startup connect timed out, and nothing ever retried - every
    # seek lost its vector leg and every vector write was dropped until the next restart
    import ast
    import logging
    import types
    from typing import Any, Dict, List, Optional, Tuple
    src = open(os.path.join(ROOT, "vera", "fabric", "data_fabric.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    node = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "FabricChromaStore")
    clock = [1000.0]
    ns = {"_chroma_bound": CB, "log": logging.getLogger("t"), "HAS_CHROMA": True, "Dict": Dict, "List": List,
          "Tuple": Tuple, "Optional": Optional, "Any": Any, "DataRecord": object,
          "time": types.SimpleNamespace(monotonic=lambda: clock[0])}
    exec(compile("from __future__ import annotations\n" + ast.get_source_segment(src, node), "df", "exec"), ns)
    store = ns["FabricChromaStore"]()
    tries = []

    def connect():
        store._last_connect = clock[0]
        tries.append(clock[0])
        if len(tries) >= 2:
            store._col = object()
            return True
        return False

    store.connect = connect
    assert store._ready() is False and len(tries) == 1          # first attempt fails
    clock[0] += 30
    assert store._ready() is False and len(tries) == 1          # within the minute: no new attempt
    clock[0] += 31
    assert store._ready() is True and len(tries) == 2           # a minute later it reconnects
    assert store._ready() is True and len(tries) == 2           # connected: no more attempts
    s = src[src.index("class FabricChromaStore:"):src.index("FABRIC_CHROMA = FabricChromaStore()")]
    assert "if not self._ready(): return []" in s and "if not self._ready(): return False" in s
