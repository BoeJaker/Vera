"""Canvas engagement events (four-minds FM-6, owner yes 17 Oct): bounded, four kinds only, read per surface."""
import asyncio
import os
import subprocess
import shutil
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from vera.canvas import engagement_core as EC  # noqa: E402

pytestmark = pytest.mark.critical


def test_clean_keeps_only_the_four_kinds_with_a_surface_bounded():
    evs = [{"event": "opened", "surface": "markets.chart", "kind": "panel", "act": "open", "key": "k1",
            "canvas": "cv_1", "session": "s1"},
           {"event": "liked", "surface": "x"},                  # not a kind we take
           {"event": "used", "surface": ""},                    # no surface
           "junk",
           {"event": "closed", "surface": "y" * 500, "act": "park"}]
    out = EC.clean(evs, session_id="fallback")
    assert [e["event"] for e in out] == ["opened", "closed"]
    assert out[0]["session_id"] == "s1" and out[1]["session_id"] == "fallback"
    assert len(out[1]["surface"]) == EC.MAX_STR
    assert len(EC.clean([{"event": "used", "surface": "s"}] * 200)) == EC.MAX_BATCH
    assert EC.clean("not a list") == []


def test_report_rates_each_surface():
    rows = [{"surface": "a", "event": "opened", "n": 3}, {"surface": "a", "event": "closed", "n": 1},
            {"surface": "b", "event": "closed", "n": 2}, {"surface": "b", "event": "restored", "n": 2},
            {"surface": "c", "event": "used", "n": 1}, {"surface": "a", "event": "bogus", "n": 9}]
    r = EC.report(rows)
    by = {s["surface"]: s for s in r["surfaces"]}
    assert by["a"]["engagement"] == 0.75 and by["a"]["total"] == 4
    assert by["b"]["engagement"] == 0.5 and by["c"]["engagement"] == 1.0
    assert [s["surface"] for s in r["surfaces"]][:2] == ["a", "b"]
    assert r["totals"]["closed"] == 3 and r["events"] == 9
    assert [s["surface"] for s in EC.report(rows, min_events=2)["surfaces"]] == ["a", "b"]


class _Conn:
    def __init__(self, store):
        self.store = store

    async def executemany(self, sql, rows):
        self.store.extend(rows)


class _Pool:
    def __init__(self, store):
        self.store = store

    def acquire(self):
        pool = self

        class _C:
            async def __aenter__(self):
                return _Conn(pool.store)

            async def __aexit__(self, *a):
                return False
        return _C()


def test_record_stores_clean_rows_and_caps_the_rate(monkeypatch):
    from vera.canvas import engagement as M
    store = []
    monkeypatch.setattr(M._orch, "PG_POOL", _Pool(store), raising=False)
    monkeypatch.setattr(M, "_DDL_DONE", True)
    monkeypatch.setattr(M, "_RATE", {"minute": 0, "n": 0})
    out = asyncio.run(M.cap_canvas_engagement_record(events=[{"event": "used", "surface": "s", "act": "pin"},
                                                             {"event": "nope", "surface": "s"}]))
    assert out == {"ok": True, "recorded": 1, "rejected": 1}
    assert store[0][:2] == ("used", "s")
    monkeypatch.setattr(M, "MAX_PER_MINUTE", 3)
    monkeypatch.setattr(M, "_RATE", {"minute": 0, "n": 0})
    out = asyncio.run(M.cap_canvas_engagement_record(events=[{"event": "used", "surface": "s"}] * 5))
    assert out["recorded"] == 3


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_the_canvas_element_mapping():
    r = subprocess.run(["node", os.path.join(ROOT, "tests", "test_canvas_engagement.cjs")], capture_output=True,
                       text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
