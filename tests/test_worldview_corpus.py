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
