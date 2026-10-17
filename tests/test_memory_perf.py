"""The memory performance log (four-minds FM-7.9): rows that do not overlap, tagged with the code
and switches that produced them, and a compare that says which way a change moved memory."""
import asyncio
import json
import os
import sys
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from vera.fabric import memory_perf_core as MP  # noqa: E402
from vera.fabric import retrieval_timing_core as rtc  # noqa: E402

pytestmark = pytest.mark.critical


def _env(path, ms, stage_ms=None, outcome="ok"):
    return {"type": "retrieval.timing", "path": path, "total_ms": ms,
            "stages": [{"stage": "embed", "ms": stage_ms if stage_ms is not None else ms, "outcome": outcome}]}


# ── Window.since ──

def test_since_reads_only_the_calls_after_the_marks():
    w = rtc.Window(size=500)
    for ms in (10, 20, 30):
        w.add(_env("memory.seek", ms))
    first, marks = w.since({})
    assert first["memory.seek"]["calls"] == 3 and first["memory.seek"]["added"] == 3
    w.add(_env("memory.seek", 400))
    w.add(_env("context.recall", 5))
    second, marks2 = w.since(marks)
    assert second["memory.seek"]["calls"] == 1 and second["memory.seek"]["total"]["p50"] == 400
    assert second["context.recall"]["calls"] == 1
    third, _ = w.since(marks2)
    assert third == {}
    # summary() still covers the whole window, as retrieval.timing has always read it
    assert w.summary()["memory.seek"]["calls"] == 4


def test_since_says_when_the_window_dropped_calls_before_they_were_read():
    w = rtc.Window(size=3)
    for ms in range(10):
        w.add(_env("p", ms))
    s, _ = w.since({})
    assert s["p"]["calls"] == 3 and s["p"]["added"] == 10


# ── rows ──

def test_a_row_carries_the_code_and_the_memory_switches_and_nothing_when_empty():
    prov = {"git_sha": "abcdef0123456789", "git_sha_short": "abcdef01", "branch": "main", "dirty": False}
    modes = {"memory.rerank": "not_chat", "memory.text_index": "fts", "plan.goal_frame": "writer"}
    paths = {"memory.seek": rtc.summarize([_env("memory.seek", 50)]), "idle": {"calls": 0}}
    r = MP.row("live", "retrieval", paths, prov, modes, "d1g", 900)
    assert r["sha_short"] == "abcdef01" and r["minds_digest"] == "d1g" and r["calls"] == 1
    assert r["memory_modes"] == {"memory.rerank": "not_chat", "memory.text_index": "fts"}
    assert list(r["paths"]) == ["memory.seek"]
    assert MP.row("live", "retrieval", {"p": {"calls": 0}}, prov, modes) is None
    with pytest.raises(ValueError):
        MP.row("other", "retrieval", paths, prov, modes)


def _rows(sha, p50, n=30, stage_p50=None):
    s = {"calls": n, "total": {"p50": p50, "p95": p50 * 2},
         "stages": {"embed": {"n": n, "p50": stage_p50 or p50, "p95": (stage_p50 or p50) * 2, "outcomes": {"ok": n}}}}
    return [{"sha_short": sha, "kind": "probe", "source": "retrieval", "minds_digest": "default",
             "calls": n, "paths": {"memory.seek": s}, "ts": "2026-10-17T00:00:00+00:00"}]


def test_compare_names_a_slower_path_and_ignores_noise():
    out = MP.compare(_rows("a", 100.0), _rows("b", 300.0, stage_p50=120.0))
    seek = out["paths"]["retrieval|memory.seek"]
    assert seek["p50"]["verdict"] == "slower" and seek["p50"]["ms"] == 200.0
    assert seek["stages"]["embed"]["p50"]["verdict"] == "slower"
    assert "slower: retrieval|memory.seek" in out["headline"]
    same = MP.compare(_rows("a", 100.0), _rows("b", 110.0))
    assert same["paths"]["retrieval|memory.seek"]["p50"]["verdict"] == "same"
    assert same["headline"] == "no p50 change past the noise floor"
    few = MP.compare(_rows("a", 100.0, n=5), _rows("b", 900.0, n=5))
    assert few["paths"]["retrieval|memory.seek"]["p50"]["verdict"] == "too_few"
    fast = MP.compare(_rows("a", 900.0), _rows("b", 100.0))
    assert fast["paths"]["retrieval|memory.seek"]["p50"]["verdict"] == "faster"


def test_pool_weights_rows_by_calls_and_versions_groups_them():
    rows = _rows("a", 100.0, n=10) + _rows("a", 200.0, n=30)
    rows[1]["ts"] = "2026-10-18T00:00:00+00:00"
    pooled = MP.pool(rows)["retrieval|memory.seek"]
    assert pooled["calls"] == 40 and pooled["total"]["p50"] == 175.0
    assert pooled["stages"]["embed"]["outcomes"] == {"ok": 40}
    v = MP.versions(rows + _rows("b", 1.0))
    assert {(e["sha_short"], e["rows"]) for e in v} == {("a", 2), ("b", 1)}
    a = [e for e in v if e["sha_short"] == "a"][0]
    assert a["first"].startswith("2026-10-17") and a["last"].startswith("2026-10-18")


# ── the capability module ──

class _Conn:
    def __init__(self, store):
        self.store = store

    async def execute(self, sql, *args):
        if sql.startswith("INSERT"):
            self.store.append(json.loads(args[5]))


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


@pytest.fixture
def mod(monkeypatch):
    from vera.fabric import memory_perf as M
    store = []
    ret, chat = rtc.Window(500), rtc.Window(500)
    monkeypatch.setattr(M._orch, "PG_POOL", _Pool(store), raising=False)
    monkeypatch.setattr(M, "_DDL_DONE", True)
    monkeypatch.setattr(M, "_STATE", {"marks": {}, "at": 0.0, "last": {}, "pruned": 1e18})
    monkeypatch.setattr(M, "_LOCK", asyncio.Lock())
    monkeypatch.setattr(M, "_provenance", lambda: {"git_sha": "f00dfeed99", "git_sha_short": "f00dfeed"})
    monkeypatch.setitem(sys.modules, "Vera.vera.fabric.memory", types.SimpleNamespace(RETRIEVAL_TIMING=ret))
    monkeypatch.setitem(sys.modules, "Vera.vera.agents.agents", types.SimpleNamespace(CHAT_TURNS=chat))

    async def effective(minds=None, **kw):
        modes = {"memory.rerank": "not_chat", "plan.goal_frame": "off"}
        modes.update(minds or {})
        src = {"memory.rerank": "global", "plan.goal_frame": "default"}
        src.update({k: "call" for k in (minds or {})})
        return {"modes": modes, "source": src}
    monkeypatch.setitem(M.CAPABILITY_REGISTRY, "minds.effective", {"func": effective})
    return types.SimpleNamespace(M=M, store=store, ret=ret, chat=chat, monkeypatch=monkeypatch)


def test_live_rows_hold_each_call_once_tagged_with_sha_and_switches(mod):
    mod.ret.add(_env("memory.seek", 40))
    mod.chat.add({"path": "vera|plain", "total_ms": 900, "stages": [{"stage": "ttft", "ms": 900, "outcome": "ok"}]})
    out = asyncio.run(mod.M.log_once())
    assert out["stored"] == 2
    by = {r["source"]: r for r in mod.store}
    assert by["retrieval"]["sha_short"] == "f00dfeed" and by["retrieval"]["kind"] == "live"
    assert by["retrieval"]["memory_modes"] == {"memory.rerank": "not_chat"}
    assert by["retrieval"]["minds_digest"] not in ("", "default")       # a switch is on globally
    assert asyncio.run(mod.M.log_once())["stored"] == 0                  # nothing new: nothing stored twice
    mod.ret.add(_env("memory.seek", 60))
    asyncio.run(mod.M.log_once())
    assert mod.store[-1]["paths"]["memory.seek"]["calls"] == 1


def test_the_probe_stores_its_own_row_and_keeps_its_calls_out_of_live_rows(mod):
    mod.ret.add(_env("memory.seek", 15))                 # live traffic before the probe

    async def known_items_run(name="v1", limit=50, minds=None, label=""):
        for ms in (100, 200, 300):
            mod.ret.add(_env("memory.seek", ms))
        return {"ok": True, "label": "default", "path": "/x.json",
                "summary": {"n": 3, "mrr": 0.5, "ndcg@10": 0.6, "hit@1": 0.3, "hit@10": 0.9, "missing": 0,
                            "ms_p50": 200.0, "ms_p95": 300.0}}
    mod.monkeypatch.setitem(mod.M.CAPABILITY_REGISTRY, "retrieval.known_items.run", {"func": known_items_run})
    out = asyncio.run(mod.M.cap_memory_perf_probe(limit=3, minds={"memory.rerank": "on"}))
    assert out["ok"] and out["stored"] == 1 and out["calls"] == 3
    live, probe = [r for r in mod.store if r["kind"] == "live"], [r for r in mod.store if r["kind"] == "probe"]
    assert live[0]["paths"]["memory.seek"]["calls"] == 1                # the call from before the probe
    assert probe[0]["extra"]["quality"]["hit@10"] == 0.9 and probe[0]["memory_modes"]["memory.rerank"] == "on"
    assert asyncio.run(mod.M.log_once())["stored"] == 0                  # probe calls never become a live row


def test_compare_needs_a_base(mod):
    out = asyncio.run(mod.M.cap_memory_perf_compare())
    assert out["ok"] is False and "base_sha" in out["error"]


def test_compare_refuses_an_unknown_head(mod):
    mod.monkeypatch.setattr(mod.M, "_provenance", lambda: {})
    out = asyncio.run(mod.M.cap_memory_perf_compare(base_sha="abc"))
    assert out["ok"] is False and "head_sha" in out["error"]


def test_the_perf_caps_stay_on_the_host_and_the_probe_may_be_scheduled():
    from vera.workers import worker_placement_core as WP
    from vera.evolve import schedule_core as SC
    for cap in ("memory.perf.probe", "memory.perf.versions", "memory.perf.compare", "memory.perf.log_now"):
        assert cap in WP.HOST_BOUND_CAPS
    assert not SC.cap_denied("memory.perf.probe")
    assert SC.cap_denied("memory.perf.log_now")            # only the probe is a scheduled job
