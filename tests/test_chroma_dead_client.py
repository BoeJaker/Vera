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
