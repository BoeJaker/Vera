"""The worldview's curated corpus (four-minds FM-8.3): no family above 15%, self-telemetry left out."""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from vera.worldview import worldview_corpus_core as WC  # noqa: E402

pytestmark = pytest.mark.critical


def test_families_and_self_telemetry():
    assert WC.family("mkt.yahoo.spy.1d") == "mkt" and WC.family("cve_nvd") == "cve"
    assert WC.family("prebaked_arxiv_ai") == "prebaked_arxiv" and WC.family("docs_bbc_com") == "docs"
    assert WC.family("agents") == "agents" and WC.family("") == "(none)"
    for d in ("caps.sandbox", "ide.claude_sessions", "streams.agent_loop_v2", "mesh.esp32-1.adc", "bus.x",
              "ts.cpu", "chat.messages", "dream.reports"):
        assert WC.excluded(d), d
    for d in ("chat.messages.archive", "dream.reports_2", "web.search", "agents", "mkt.yahoo.spy.1d"):
        assert not WC.excluded(d), d


def test_quotas_water_fill_under_the_cap():
    q = WC.quotas({"big": 200000, "mid": 5000, "small": 100, "tiny": 3}, 10000)
    assert q["tiny"] == 3 and q["small"] == 100 and q["big"] == 1500 and q["mid"] == 1500   # cap 15% x 10,000
    many = {f"f{i}": 10000 for i in range(10)}
    q = WC.quotas(many, 10000)
    assert sum(q.values()) == 10000 and max(q.values()) <= 1500
    assert WC.quotas({}, 100) == {} and WC.quotas({"a": 5}, 0) == {}


def test_the_plan_and_the_realised_mix():
    counts = {"caps.sandbox": 207008, "ide.claude_sessions": 148176, "agents_archive": 87265, "agents": 28490,
              "mkt.yahoo.spy.1d": 8492, "mkt.binance.xrp_btc.1h": 78228, "web.search": 16477, "cve_nvd": 2500}
    p = WC.plan(counts, 20000)
    assert p["excluded"] == ["caps.sandbox", "ide.claude_sessions"]
    assert set(p["datasets"]) == {"agents", "mkt", "web", "cve"} and sorted(p["datasets"]["mkt"]) == [
        "mkt.binance.xrp_btc.1h", "mkt.yahoo.spy.1d"]
    assert max(p["quotas"].values()) <= 3000 and p["eligible"] == 87265 + 28490 + 8492 + 78228 + 16477 + 2500
    m = WC.mix(["a"] * 15 + ["b"] * 5 + ["mkt.x"] * 80)
    assert m["records"] == 100 and m["max_dataset_share"] == 0.8 and m["max_family_share"] == 0.8
    assert m["top_families"][0] == ["mkt", 80] and WC.mix([]) == {"records": 0}


def test_training_uses_the_curated_pick_and_keeps_the_mix():
    src = open(os.path.join(ROOT, "vera", "worldview", "worldview_jepa.py"), encoding="utf-8").read()
    body = src[src.index("async def cap_worldview_train("):]
    assert body.index("_curated_corpus_ids(limit)") < body.index("graph = await build_graph(dataset_id")
    assert "MODEL.corpus = _wv_corpus.mix(" in body
    pick = src[src.index("async def _curated_corpus_ids("):src.index("async def _sqlite_records_for_dataset(")]
    assert "FROM fabric_records" in pick and "run_in_executor" in pick and "_wv_corpus.plan(" in pick
    assert '"corpus":            dict(self.corpus or {}),' in src and '"curated_corpus":     True,' in src


def test_interleave_keeps_every_family_when_cut_short():
    ids = WC.interleave({"b": ["b1", "b2", "b3"], "a": ["a1"], "c": ["c1", "c2"], "empty": []})
    assert ids == ["a1", "b1", "c1", "b2", "c2", "b3"]
    assert set(ids[:3]) == {"a1", "b1", "c1"} and WC.interleave({}) == []
    assert WC.batches(list("abcde"), 2) == [["a", "b"], ["c", "d"], ["e"]] and WC.batches([], 3) == []


def test_family_take_holds_each_quota_and_the_limit():
    t = WC.FamilyTake({"mkt": 2, "cve": 1}, limit=3)
    got = [t.take(d) for d in ("mkt.a", "mkt.b", "mkt.c", "cve_nvd", "web.search", "cve_x")]
    assert got == [True, True, False, True, False, False] and t.full and t.n == 3


def _getter(store, fail_on=()):
    calls = []

    def get(batch):
        calls.append(list(batch))
        if len(calls) in fail_on:
            raise TimeoutError("timed out")
        have = [i for i in batch if i in store]
        return have, [[0.0] * 12 for _ in have], {i: {"dataset_id": store[i]} for i in have}
    return get, calls


def test_fetch_by_ids_batches_and_skips_missing_vectors():
    store = {f"r{i}": ("mkt.x" if i % 2 else "cve_nvd") for i in range(10) if i != 3}
    get, calls = _getter(store)
    rids, rows, meta, info = WC.fetch_by_ids(get, [f"r{i}" for i in range(10)], size=4)
    assert [len(c) for c in calls] == [4, 4, 2] and "r3" not in rids and len(rids) == len(rows) == 9
    assert info == {"batches": 3, "failed": 0, "fetched": 9} and meta["r1"]["dataset_id"] == "mkt.x"


def test_a_failed_batch_is_skipped_and_three_in_a_row_stop():
    store = {f"r{i}": "mkt.x" for i in range(12)}
    get, _ = _getter(store, fail_on=(2,))
    fails = []
    rids, _, _, info = WC.fetch_by_ids(get, list(store), size=4, on_fail=lambda n, k, e: fails.append((n, k)))
    assert len(rids) == 8 and info["failed"] == 1 and fails == [(4, 1)]
    get, calls = _getter(store, fail_on=(1, 2, 3))
    rids, _, _, info = WC.fetch_by_ids(get, list(store), size=2)
    assert rids == [] and len(calls) == 3 and info["failed"] == 3


def test_fetch_by_ids_stops_when_the_quotas_are_full():
    store = {f"m{i}": "mkt.x" for i in range(50)}
    store.update({f"c{i}": "cve_nvd" for i in range(50)})
    order = WC.interleave({"mkt": [f"m{i}" for i in range(50)], "cve": [f"c{i}" for i in range(50)]})
    take = WC.FamilyTake({"mkt": 5, "cve": 3}, limit=8)
    get, calls = _getter(store)
    rids, _, _, _ = WC.fetch_by_ids(get, order, size=4, take=take)
    assert len(rids) == 8 and sum(r.startswith("m") for r in rids) == 5 and len(calls) < 25
    rids, _, _, _ = WC.fetch_by_ids(_getter(store)[0], list(store), size=7, cap=10)
    assert len(rids) == 10


def test_training_fetches_the_curated_vectors_in_batches():
    src = open(os.path.join(ROOT, "vera", "worldview", "worldview_jepa.py"), encoding="utf-8").read()
    fetch = src[src.index("async def _fetch_records_with_embeddings("):src.index("async def _fetch_loom_edges(")]
    assert "_wv_corpus.fetch_by_ids(" in fetch and "kwargs[\"ids\"]" not in fetch
    pick = src[src.index("async def _curated_corpus_ids("):src.index("async def _sqlite_records_for_dataset(")]
    assert "_wv_corpus.interleave(groups)" in pick
    train = src[src.index("async def cap_worldview_train("):]
    assert "curated_take = _wv_corpus.FamilyTake(" in train and "take=curated_take)" in train
    assert '"fetch_batch":        500,' in src
