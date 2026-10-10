"""Decisions run on the nodes through the NLP servers' POST /decide (four-minds FM-9.1).

The worker nodes are the compute pool: the nearest-neighbour search behind the kNN judge and the tool shortlist
moved off the Vera host's event loop (pure Python, ~110 ms per 3,000-row vote, measured 2026-10-10) onto the NLP
servers (numpy). The host pushes an index once, asks for neighbours, and votes over the k it gets back.
"""
import asyncio
import os
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "edge"))
sys.path.insert(0, os.path.join(_ROOT, "vera", "research"))

from vera.minds import decide_dispatch as DD, judge_core as JC, tool_shortlist_core as TC  # noqa: E402

pytestmark = pytest.mark.critical
np = pytest.importorskip("numpy")


def _ns():
    pytest.importorskip("fastapi", reason="edge servers need fastapi")
    ns = pytest.importorskip("nlp_server")
    ns._DECIDE.clear()
    return ns


ROWS = [{"id": "a", "vec": [1.0, 0.0, 0.0], "run": "r1", "payload": "met"},
        {"id": "b", "vec": [0.9, 0.1, 0.0], "run": "r2", "payload": "met"},
        {"id": "c", "vec": [0.0, 1.0, 0.0], "run": "r3", "payload": "not_met"},
        {"id": "d", "vec": [0.8, 0.2, 0.0], "run": "self", "payload": "not_met"}]


def test_the_node_keeps_an_index_and_answers_nearest_first():
    ns = _ns()
    assert ns.decide_index_put("i", "v1", ROWS) == {"ok": True, "index_id": "i", "version": "v1", "rows": 4, "dim": 3}
    out = ns.decide_knn("i", "v1", [1.0, 0.0, 0.0], k=2, exclude_run="self")
    assert out["ok"] and [n["id"] for n in out["neighbours"]] == ["a", "b"] and out["eligible"] == 3
    assert out["neighbours"][0]["payload"] == "met" and out["neighbours"][0]["run"] == "r1"
    assert ns.decide_knn("i", "v2", [1, 0, 0])["error"] == "index_missing"          # stale version
    assert ns.decide_knn("nope", "", [1, 0, 0])["error"] == "index_missing"
    assert "dims" in ns.decide_knn("i", "v1", [1, 0])["error"]
    assert ns.decide_knn("i", "v1", [1, 0, 0], k=50)["eligible"] == 4 and len(ns.decide_knn("i", "v1", [1, 0, 0], k=50)["neighbours"]) == 4
    assert ns.decide_inventory() == {"i": {"version": "v1", "rows": 4, "dim": 3}}
    assert ns.decide_index_put("", "v", ROWS)["error"] and ns.decide_index_put("x", "v", [])["error"]


def test_the_node_drops_its_oldest_index_past_the_cap(monkeypatch):
    ns = _ns()
    monkeypatch.setattr(ns, "DECIDE_MAX_INDEXES", 2)
    for i in range(3):
        ns.decide_index_put(f"i{i}", "v", ROWS)
    assert sorted(ns.decide_inventory()) == ["i1", "i2"]


def test_the_endpoints_and_the_handshake(monkeypatch):
    ns = _ns()
    from fastapi.testclient import TestClient
    monkeypatch.setattr(ns, "run_embed", lambda texts: {"ok": True, "model": "m", "embeddings": [[1.0, 0.0, 0.0]]})
    with TestClient(ns.build_app()) as c:
        h = c.get("/health").json()["decide"]
        assert h["schema"] == "vera.decide/v1" and h["methods"] == ["knn"] and h["indexes"] == {}
        assert c.post("/decide", json={"index_id": "i", "version": "v1", "text": "x"}).json()["error"] == "index_missing"
        assert c.post("/decide/index", json={"index_id": "i", "version": "v1", "rows": ROWS}).json()["ok"]
        r = c.post("/decide", json={"index_id": "i", "version": "v1", "text": "form", "k": 3}).json()
        assert r["ok"] and r["neighbours"][0]["id"] == "a" and r["embed_model"] == "m"
        assert c.post("/decide", json={"method": "train"}).json()["error"].startswith("method must be")
        assert c.get("/health").json()["decide"]["indexes"]["i"]["rows"] == 4


def test_the_votes_over_node_neighbours_match_the_host_ones():
    pool = [{"vec": r["vec"], "answer": r["payload"], "run": r["run"]} for r in ROWS] + [
        {"vec": [0.7, 0.3, 0.0], "answer": "met", "run": "r4"}, {"vec": [0.1, 0.9, 0.0], "answer": "not_met", "run": "r5"}]
    host = JC.knn_vote("loop.step_verify", [1.0, 0.0, 0.0], pool, exclude_run="self", k=3, min_neighbours=5)
    near = sorted(((sum(a * b for a, b in zip(JC._unit([1, 0, 0]), JC._unit(r["vec"]))), r["answer"])
                   for r in pool if r["run"] != "self"), reverse=True)[:3]
    assert JC.vote_from_neighbours("loop.step_verify", near, 5, min_neighbours=5)[0] == host[0]
    assert JC.vote_from_neighbours("loop.step_verify", near, 4, min_neighbours=5) == (None, None, 4)
    idx = [{"run": "a", "vec": [1, 0], "tools": ["x", "y"]}, {"run": "b", "vec": [0, 1], "tools": ["z"]}]
    host_t = TC.shortlist([1, 0], idx, k=3, neighbours=1)
    assert TC.rank_from_neighbours([(1.0, ["x", "y"])], TC.prior(idx), 3) == host_t


class _ND:
    """A fake nlp_dispatch: one decide-capable node that has no index until it is pushed one."""
    def __init__(self, decide=True, nlp_local=False):
        self.posts, self.index = [], None
        self.node = {"node_id": "cpu-246", "nlp_url": "http://n:8771",
                     "decide": {"schema": "vera.decide/v1", "methods": ["knn"]} if decide else {}}
        self.cfg = {"nlp_local": nlp_local, "node": ""}

    async def get_config(self):
        return self.cfg

    async def discover(self, force=False):
        return [self.node, {"node_id": "old", "nlp_url": "http://o:8771"}]

    def pick_nlp_node(self, servers):
        return (servers[0], "best") if servers else (None, "none")

    async def _post_json(self, url, path, body, timeout=0):
        self.posts.append(path)
        if path == "/decide/index":
            self.index = body["version"]
            return {"ok": True}
        if self.index != body["version"]:
            return {"error": "index_missing"}
        return {"ok": True, "neighbours": [{"id": "a", "score": 0.9, "run": "r1", "payload": "met"}], "eligible": 3, "ms": 0.4}


def test_the_host_pushes_a_missing_index_once_and_never_computes_by_default(monkeypatch):
    fake = _ND()
    monkeypatch.setattr(DD, "ND", fake)

    async def rows():
        return ROWS
    out = asyncio.run(DD.knn("i", "v1", rows, vector=[1, 0, 0], k=3))
    assert out["ok"] and out["node"] == "cpu-246" and fake.posts == ["/decide", "/decide/index", "/decide"]
    asyncio.run(DD.knn("i", "v1", rows, vector=[1, 0, 0]))
    assert fake.posts[-1] == "/decide" and fake.posts.count("/decide/index") == 1
    monkeypatch.setattr(DD, "ND", _ND(decide=False))                     # the old server never gets the call
    out = asyncio.run(DD.knn("i", "v1", rows, text="x"))
    assert out["error"].startswith("no NLP node") and out["local_allowed"] is False
    assert asyncio.run(DD.local_allowed()) is False
    monkeypatch.setattr(DD, "ND", _ND(decide=False, nlp_local=True))
    assert asyncio.run(DD.local_allowed()) is True
    assert DD.index_version(["a", "b"]) != DD.index_version(["b", "a"]) and len(DD.index_version([])) == 16


def test_the_callers_ask_a_node_and_compute_only_when_allowed_and_off_the_loop():
    src = lambda *p: open(os.path.join(_ROOT, *p), encoding="utf-8").read()
    sl = src("vera", "minds", "tool_shortlist.py")
    body = sl[sl.index("async def cap_caps_shortlist"):]
    assert body.index("DD.knn(\"tool_steps\"") < body.index("DD.local_allowed()") < body.index("asyncio.to_thread(TC.shortlist")
    jd = src("vera", "minds", "judge.py")
    kb = jd[jd.index("async def cap_decide_shadow_knn"):jd.index("async def _night_job")]
    assert "DD.knn(index_id, version, _push_rows" in kb and "asyncio.to_thread(JC.knn_vote" in kb
    assert "JC.knn_vote(question_id, it[\"vec\"], pool" not in kb.replace("asyncio.to_thread(JC.knn_vote", "")
    assert '"decide": health.get("decide") or {}' in src("vera", "research", "nlp_dispatch.py")
