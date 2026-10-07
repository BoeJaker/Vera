import json
from pathlib import Path

import pytest

from vera.discovery_benchmark import SCHEMA as BENCHMARK_SCHEMA
from vera.discovery_operator_readmodel import (
    DiscoveryOperatorLedger, project_benchmark_comparison,
)


pytestmark = pytest.mark.critical


def comparison(secret="secret query payload"):
    summary = {"samples": 2, "completed": 2, "unsuccessful_rate": 0.0,
               "useful_context_rate": 1.0, "quality": {"ndcg": 0.8},
               "latency_ms": {"first_useful_p95": 20},
               "resources": {"bytes": 10}, "policy_violations": 0,
               "secret": secret}
    return {"schema": BENCHMARK_SCHEMA, "fixture_id": "fixture-1",
            "snapshot_id": "snap_abc", "baseline_variant_id": "base",
            "candidate_variant_id": "candidate",
            "summaries": {"base": summary, "candidate": summary},
            "blockers": [], "passed": True,
            "observation_ids": ["one", "two"],
            "case_results": {"case": {"payload": secret}}}


def test_benchmark_projection_is_payload_free_and_operator_useful():
    value = project_benchmark_comparison(comparison())
    assert value["passed"] is True
    assert value["observation_count"] == 2
    assert value["summaries"]["candidate"]["quality"]["ndcg"] == 0.8
    assert "secret query payload" not in json.dumps(value)


def test_ledger_is_bounded_newest_first_and_returns_copies():
    ledger = DiscoveryOperatorLedger(capacity=2)
    for index in range(3):
        value = comparison()
        value["fixture_id"] = f"fixture-{index}"
        ledger.record_benchmark(value)
    snap = ledger.snapshot()
    assert snap["counts"] == {"routes": 0, "benchmarks": 2}
    assert [value["fixture_id"] for value in snap["benchmarks"]] == [
        "fixture-2", "fixture-1"]
    snap["benchmarks"][0]["fixture_id"] = "forged"
    assert ledger.snapshot()["benchmarks"][0]["fixture_id"] == "fixture-2"
    assert snap["payloads_included"] is False
    assert snap["control_authority"] is False


def test_invalid_comparisons_and_limits_fail_closed():
    with pytest.raises(ValueError, match="context benchmark"):
        project_benchmark_comparison({"schema": "wrong"})
    bad = comparison()
    bad["blockers"] = ["x" * 257]
    with pytest.raises(ValueError, match="blockers"):
        project_benchmark_comparison(bad)
    bad = comparison()
    bad["summaries"]["candidate"]["quality"] = {"ndcg": "raw payload"}
    with pytest.raises(ValueError, match="finite numbers"):
        project_benchmark_comparison(bad)
    bad = comparison()
    bad["observation_ids"] = "payload-shaped-string"
    with pytest.raises(ValueError, match="observation ids"):
        project_benchmark_comparison(bad)
    bad = comparison()
    bad["passed"] = "false"
    with pytest.raises(ValueError, match="must be boolean"):
        project_benchmark_comparison(bad)
    bad = comparison()
    bad["blockers"] = ["unsafe\nvalue"]
    with pytest.raises(ValueError, match="blockers"):
        project_benchmark_comparison(bad)
    with pytest.raises(ValueError, match="capacity"):
        DiscoveryOperatorLedger(0)
    with pytest.raises(ValueError, match="limit"):
        DiscoveryOperatorLedger().snapshot(0)


def test_operator_panel_exposes_read_only_discovery_evidence():
    operator_dir = Path(__file__).parents[1] / "vera" / "operator"
    panel = (operator_dir /
             "operator_studio_panel.html").read_text(encoding="utf-8")
    capabilities = (operator_dir / "operator_web_capabilities.py").read_text(
        encoding="utf-8")
    assert 'id="discEvList"' in panel
    assert "/operator/discovery/evidence?limit=10" in panel
    assert "this view cannot run discovery" in panel
    assert '"operator.discovery.evidence"' in capabilities
    assert '"operator.discovery.benchmark.record"' in capabilities
