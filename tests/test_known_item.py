"""The known-item retrieval eval set (four-minds FM-7.0c): queries from a record's own opening words, a stratified
deterministic pick, and the ranking numbers seek changes are judged by."""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from vera.fabric import known_item_core as KI  # noqa: E402

pytestmark = pytest.mark.critical
LONG = " and more text to pass the minimum length of a usable record body." * 3


def test_query_from_keeps_distinctive_opening_words():
    assert KI.query_from("The Valkey fork of Redis: what changed after the licence change in 2024") == \
        "valkey fork redis changed licence change"
    assert KI.query_from("the and of to it is") == "" and KI.query_from("") == ""
    assert KI.query_from("Kubernetes kubernetes KUBERNETES pods nodes") == "kubernetes pods nodes"


def test_pick_is_stratified_deterministic_and_skips_telemetry():
    cands = ([{"id": f"m{i}", "dataset_id": "mkt.x", "text": f"Bitcoin price candle series number {i}" + LONG}
              for i in range(50)]
             + [{"id": f"c{i}", "dataset_id": "cve_nvd", "text": f"Remote execution vulnerability advisory {i}" + LONG}
                for i in range(50)]
             + [{"id": f"s{i}", "dataset_id": "caps.sandbox", "text": f"Sandbox command output listing {i}" + LONG}
                for i in range(50)]
             + [{"id": "short", "dataset_id": "web.x", "text": "too short"}])
    a = KI.pick(cands, 10, seed="t")
    assert a == KI.pick(list(reversed(cands)), 10, seed="t")          # order of input does not matter
    assert len(a) == 10 and not any(it["dataset_id"].startswith("caps.") for it in a)
    fams = {it["family"] for it in a}
    assert fams == {"mkt", "cve"} and all(it["query"] for it in a)
    assert sum(1 for it in a if it["family"] == "mkt") == 5          # two families: an equal share each
    many = [{"id": f"f{f}-{i}", "dataset_id": f"fam{f}.x", "text": f"Distinct topic number {f} record {i}" + LONG}
            for f in range(10) for i in range(10)]
    capped = KI.pick(many, 20, seed="t", cap=0.15)
    per = {}
    for it in capped:
        per[it["family"]] = per.get(it["family"], 0) + 1
    assert len(capped) == 20 and max(per.values()) <= 3                # ten families: none above 15% of 20
    assert KI.pick(cands, 10, seed="other") != a


def test_rank_and_summary():
    assert KI.rank_of("b", ["a", "b", "c"]) == 2 and KI.rank_of("z", ["a"]) is None and KI.rank_of("a", []) is None
    s = KI.summarize([1, 2, None, 11], [100.0, 200.0, 300.0, 400.0])
    assert s["n"] == 4 and s["mrr"] == round((1 + 0.5 + 0 + 1 / 11) / 4, 3)
    assert s["hit@1"] == 0.25 and s["hit@5"] == 0.5 and s["hit@10"] == 0.5 and s["missing"] == 1
    assert s["ndcg@10"] == round((1 + 1 / 1.584962500721156) / 4, 3)
    assert s["ms_p50"] == 200.0 and s["ms_p95"] == 400.0
    assert KI.summarize([]) == {"n": 0}


def test_compare_arms():
    base = KI.summarize([2, 2], [100, 100])
    arm = KI.summarize([1, 1], [150, 150])
    d = KI.compare(base, arm)
    assert d["mrr"] == 0.5 and d["hit@1"] == 1.0 and d["ms_p50"] == 50


def test_the_run_forces_the_cache_off_and_uses_the_run_layer():
    src = open(os.path.join(ROOT, "vera", "fabric", "known_item.py"), encoding="utf-8").read()
    assert 'modes["memory.seek_cache"] = "off"' in src and "SB.RUN_MINDS.set(" in src and "SB.RUN_MINDS.reset(token)" in src
    assert "include_memory=False" in src
    assert 'os.path.join(_here, "fabric/known_item.py")' in open(
        os.path.join(ROOT, "vera", "capability_orchestration.py"), encoding="utf-8").read()


def test_the_set_is_sampled_from_what_seek_searches():
    src = open(os.path.join(ROOT, "vera", "fabric", "known_item.py"), encoding="utf-8").read()
    assert 'source: str = "pg"' in src and "TABLESAMPLE SYSTEM" in src and "FROM fabric_records" in src
    build = src[src.index("async def cap_known_items_build("):src.index("async def cap_known_items_run(")]
    assert build.index('if source == "sqlite":') < build.index("await _candidates_pg(")


def test_a_run_is_a_w5_03_evaluation_report_bound_to_its_cases():
    # backbone review 11 Oct: one evaluation contract (vera/models/training_contracts EvaluationReport)
    from vera.fabric import known_item_core as K
    items = [{"id": "r1", "query": "alpha beta gamma"}, {"id": "r2", "query": "delta epsilon zeta"}]
    rev = K.set_revision(items)
    assert rev == K.set_revision(list(reversed(items))) != K.set_revision(items[:1]) and rev.startswith("known_items:")
    assert K.subject_id({"memory.rerank": "on"}) == "memory.seek:memory.rerank:on" and K.subject_id(None) == "memory.seek"
    base = {"n": 2, "mrr": 0.5, "ndcg@10": 0.6, "hit@10": 0.5, "missing": 1}
    arm = {"n": 2, "mrr": 0.75, "ndcg@10": 0.8, "hit@10": 1.0, "missing": 0}
    rep = K.evaluation_report(rev, {"memory.rerank": "on"}, arm, baseline=base)
    assert rep["schema"] == "vera.evaluation-report/v1" and rep["status"] == "completed" and rep["passed"]
    assert rep["request"]["dataset_revision_id"] == rev and rep["evaluated_cases"] == 2
    assert {m["metric_id"]: m["threshold"] for m in rep["metrics"]} == {"hit_at_10": 0.5, "mrr": 0.5, "ndcg_at_10": 0.6}
    worse = K.evaluation_report(rev, None, base, baseline=arm)
    assert not worse["passed"] and worse["failed_cases"] == 1
    assert K.evaluation_report(rev, None, {"n": 0})["status"] == "failed"
