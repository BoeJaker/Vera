"""The node crawl class (vera/fabric/ingress_crawl_core.py, worker placement; web-ingress WI-12a)."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vera.fabric import ingress_crawl_core as cc  # noqa: E402
from vera.workers import worker_placement_core as wp  # noqa: E402


def test_batches_are_one_host_each_interleaved_and_deduplicated():
    urls = ["https://a.example/1", "https://a.example/2", "https://www.a.example/3", "https://a.example/4",
            "https://b.example/1", "https://a.example/1#frag", "ftp://x/y", "", "https://c.example/1"]
    b = cc.plan_batches(urls, per_batch=3)
    assert b == [["https://a.example/1", "https://a.example/2", "https://www.a.example/3"], ["https://b.example/1"],
                 ["https://c.example/1"], ["https://a.example/4"]]
    assert cc.plan_batches([]) == [] and len(cc.plan_batches(["https://a.example/%d" % i for i in range(30)], 99)[0]) == 10


def test_fetch_rows_travel_text_only_and_never_binary():
    r = cc.fetch_row("https://a.example/", status=200, content_type="text/html; charset=utf-8", body="<p>hi</p>")
    assert r["html"] == "<p>hi</p>" and r["text"] == "" and r["bytes"] == 9
    j = cc.fetch_row("https://a.example/x.json", status=200, content_type="application/json", body='{"a": 1}')
    assert j["text"] == '{"a": 1}' and j["html"] == ""
    pdf = cc.fetch_row("https://a.example/x.pdf", status=200, content_type="application/pdf", body="%PDF-1.7 ...")
    assert pdf["text"] == "" and pdf["html"] == ""
    garbled = cc.fetch_row("https://a.example/", status=200, content_type="text/html", body="\ufffd\x05\x00" * 50)
    assert garbled["html"] == "" and garbled["error"] == "binary body"
    assert len(cc.fetch_row("https://a.example/", status=200, content_type="text/plain", body="x" * 50, max_chars=10)["text"]) == 10


def test_summary_and_no_url_is_lost():
    batches = [{"ran_on": "node:cpu-246", "attempts": 1, "results": [cc.fetch_row("https://a.example/1", status=200, content_type="text/html", body="x")]},
               {"ran_on": "host:vera", "attempts": 2, "results": [cc.failed_row("https://b.example/1", "timeout")]}]
    s = cc.summary(batches)
    assert s == {"ran_on": {"node:cpu-246": 1, "host:vera": 1}, "fetched": 1, "failed": 1, "retried": 1}
    assert cc.lost(["https://a.example/1", "https://b.example/1"], batches) == []
    assert cc.lost(["https://a.example/1", "https://c.example/9"], batches) == ["https://c.example/9"]
    assert cc.where("cpu-246", True) == "node:cpu-246" and cc.where("", False) == "host:?"


def test_the_crawl_class_runs_ingress_fetches_and_nothing_else():
    assert "crawl" in wp.CLASSES and wp.CLASSES["crawl"]["namespaces"] == ("ingress",)
    assert wp.class_of("ingress.fetch.batch", env={}) == "crawl"
    assert wp.placement("ingress.fetch.batch")[0] == "any"
    assert wp.class_of("fabric.crawl.fetch_many", env={}) == ""          # the fan-out and the receipts stay home
    assert wp.placement("ingress.fetch.set")[0] == "host"                  # the mutator rule still applies
    assert "crawl" not in wp.DEFAULT_CLASSES_CPU                           # a node gets it only by its roles
    assert "crawl" in wp.stage_of(5)["classes"] and "crawl" not in wp.stage_of(2)["classes"]


def test_the_caps_fetch_behind_the_shared_slot_and_record_on_the_host():
    import ast
    src = (ROOT / "vera" / "fabric" / "ingress_capabilities.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fb = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "_fetch_batch")
    seg = ast.get_source_segment(src, fb)
    assert "_host_slot(" in seg and "accept_encoding()" in seg and "is_internal(" in seg
    assert "record_fetch_result" not in seg and "WRITER" not in seg                 # nothing recorded on the node
    fm = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "cap_crawl_fetch_many")
    seg = ast.get_source_segment(src, fm)
    assert '_call("ingress.fetch.batch"' in seg and "record_fetch_result" in seg and "for attempts in (1, 2)" in seg



def test_a_crawl_worker_reserves_for_itself_while_a_batch_runs():
    from vera.workers import node_compute_core as ncc
    assert ncc.NEEDS["ingress.fetch.batch"] == {"cpu": 0.25, "mem_gb": 0.25} and cc.batch_timeout_s(3) == 105
    import ast
    src = (ROOT / "vera" / "fabric" / "ingress_capabilities.py").read_text(encoding="utf-8")
    fn = next(n for n in ast.walk(ast.parse(src)) if isinstance(n, ast.AsyncFunctionDef) and n.name == "cap_ingress_fetch_batch")
    seg = ast.get_source_segment(src, fn)
    assert "_reserve_self(" in seg and "finally:" in seg and "_release_self(" in seg       # released on error too
    rs = next(n for n in ast.walk(ast.parse(src)) if isinstance(n, ast.AsyncFunctionDef) and n.name == "_reserve_self")
    seg = ast.get_source_segment(src, rs)
    assert "_IS_WORKER" in seg and "hset" in seg and "extra_entry(cc.BATCH_FN" in seg and "expire" not in seg
    assert "KEY_EXTRA_RES" in seg                                                            # the key P0d's admit() reads
    import json as _j
    e = _j.loads(ncc.extra_entry("ingress.fetch.batch", cc.batch_timeout_s(3), 1000.0))
    assert e == {"cpu": 0.25, "mem_gb": 0.25, "until": 1105.0, "cap": "ingress.fetch.batch"}



def test_the_batch_cap_takes_the_timeout_an_offloaded_call_carries():
    # 18 Oct probe: every batch sent to cpu-246 failed with "unexpected keyword argument '_timeout'"
    import ast
    src = (ROOT / "vera" / "fabric" / "ingress_capabilities.py").read_text(encoding="utf-8")
    fn = next(n for n in ast.walk(ast.parse(src)) if isinstance(n, ast.AsyncFunctionDef) and n.name == "cap_ingress_fetch_batch")
    assert "_timeout" in [a.arg for a in fn.args.args]
