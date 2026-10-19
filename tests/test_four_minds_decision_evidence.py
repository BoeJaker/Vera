from copy import deepcopy

import pytest

from vera.minds import ledger_core as LC
from vera.minds.evidence_core import SCHEMA, project_decision_evidence


pytestmark = pytest.mark.critical


def _decision(**overrides):
    row = LC.make_decision(
        "loop.step_verify", "yes", origin="user", surface="agent_loop",
        qtype="binary", labels=["yes", "no"], state={"secret": "payload"},
        state_ref={"store": "decision_states", "id": "state-7"},
        decider="student", implementation="knn:v3", mode="student_first",
        p=0.98, package="student.knn", node="cpu-246", ms=12,
        minds_digest="switches-v9", run_id="run-7", session_id="session-3",
        detail={"private": "evidence"}, ts="2026-10-18T20:00:00Z")
    row.update(overrides)
    return row


def _outcome(decision_id, **overrides):
    row = LC.make_outcome(
        decision_id, "correct", "run.gate", "strong",
        detail={"private": "outcome"}, ts="2026-10-18T20:01:00Z")
    row.update(overrides)
    return row


def _route(**overrides):
    row = {
        "kind": "nlp_knn", "node_id": "cpu-246", "index_id": "decisions.loop-step-verify",
        "index_version": "b71aa9", "route_revision": "nlp-route-12",
        "round_trip_ms": 19, "ms": 2,
    }
    row.update(overrides)
    return row


def test_projection_joins_exact_identities_without_payload_or_authority():
    decision = _decision()
    result = project_decision_evidence(
        decision, outcomes=[_outcome(decision["decision_id"])], route=_route(),
        timing={"sense_ms": 4, "total_ms": 35},
        context_evidence_ids=["ctxrev_4", "jepaev_8"], require_context=True)
    assert result["schema"] == SCHEMA
    assert result["eligible_for_evaluation"] is True
    assert result["missing"] == []
    assert result["decision"]["decision_id"] == decision["decision_id"]
    assert result["route"]["node_id"] == "cpu-246"
    assert result["route"]["index_version"] == "b71aa9"
    assert result["timing_ms"] == {
        "sense_ms": 4.0, "dispatch_ms": 19.0, "compute_ms": 2.0,
        "decision_ms": 12.0, "total_ms": 35.0,
    }
    assert result["context_evidence_ids"] == ["ctxrev_4", "jepaev_8"]
    assert result["payload_retained"] is False
    assert result["executes"] is result["writes"] is False
    assert result["changes_routing"] is result["changes_authority"] is False
    assert result["activation_authority"] is False
    rendered = repr(result)
    for secret in ("secret payload", "private evidence", "private outcome",
                   "'correct'", "'yes'"):
        assert secret not in rendered


def test_projection_is_stable_under_outcome_and_context_order():
    decision = _decision()
    outcomes = [
        _outcome(decision["decision_id"], label="partial", strength="medium",
                 ts="2026-10-18T20:00:30Z"),
        _outcome(decision["decision_id"]),
    ]
    first = project_decision_evidence(
        decision, outcomes=outcomes, route=_route(),
        context_evidence_ids=["jepaev_8", "ctxrev_4"])
    second = project_decision_evidence(
        decision, outcomes=list(reversed(outcomes)), route=_route(),
        context_evidence_ids=["ctxrev_4", "jepaev_8"])
    assert first == second
    assert first["strongest_outcome"]["strength"] == "strong"
    assert first["evidence_id"].startswith("fmde_")


def test_missing_links_remain_explicit_and_never_grant_readiness():
    result = project_decision_evidence(
        _decision(package=None, node=None, implementation="card:minilm"),
        route={"kind": "nlp_model", "node_id": "cpu-247"},
        require_context=True, require_model_package=True)
    assert result["eligible_for_evaluation"] is False
    assert result["missing"] == [
        "context_evidence_ids", "outcome", "route.deployment_id",
        "route.model_package_id", "route.provider_id",
    ]
    assert result["changes_authority"] is False


def test_student_or_model_decisions_cannot_default_to_a_native_route():
    decision = _decision()
    result = project_decision_evidence(
        decision, outcomes=[_outcome(decision["decision_id"])])
    assert result["eligible_for_evaluation"] is False
    assert result["missing"] == ["route.kind_for_decider"]
    with pytest.raises(ValueError, match="route node does not match decision node"):
        project_decision_evidence(
            decision, outcomes=[_outcome(decision["decision_id"])],
            route={**_route(), "node_id": "cpu-247"})


def test_model_routes_require_portable_package_and_deployment_identity():
    decision = _decision(implementation="card:minilm", node="cpu-247")
    route = {
        "kind": "nlp_model", "node_id": "cpu-247", "provider_id": "nlp-node",
        "deployment_id": "dep-19", "model_package_id": "mpkg-21",
        "model_revision": "rev-5", "route_revision": "route-9",
    }
    result = project_decision_evidence(
        decision, outcomes=[_outcome(decision["decision_id"])], route=route,
        require_model_package=True)
    assert result["eligible_for_evaluation"] is True
    assert result["route"]["model_package_id"] == "mpkg-21"
    assert result["route"]["deployment_id"] == "dep-19"


def test_forged_or_malformed_evidence_fails_closed():
    decision = _decision()
    with pytest.raises(ValueError, match="invalid decision row"):
        project_decision_evidence({**decision, "labels_hash": "forged"})
    with pytest.raises(ValueError, match="does not match decision"):
        project_decision_evidence(
            decision, outcomes=[_outcome("dec_" + "0" * 32)], route=_route())
    with pytest.raises(ValueError, match="unique"):
        project_decision_evidence(
            decision, route=_route(), context_evidence_ids=["ctx_1", "ctx_1"])
    with pytest.raises(ValueError, match="sequence"):
        project_decision_evidence(
            decision, route=_route(), context_evidence_ids="ctx_1")
    outcome = _outcome(decision["decision_id"])
    with pytest.raises(ValueError, match="sequence of outcome objects"):
        project_decision_evidence(decision, outcomes=outcome, route=_route())
    with pytest.raises(ValueError, match="outcome evidence must be unique"):
        project_decision_evidence(
            decision, outcomes=[outcome, deepcopy(outcome)], route=_route())
    with pytest.raises(ValueError, match="unsupported route kind"):
        project_decision_evidence(decision, route={"kind": "magic"})
    with pytest.raises(ValueError, match="unsupported timing fields"):
        project_decision_evidence(decision, route=_route(), timing={"wall_clock": 2})
    with pytest.raises(ValueError, match="non-negative finite"):
        project_decision_evidence(decision, route=_route(), timing={"total_ms": -1})


def test_projection_does_not_mutate_inputs():
    decision = _decision()
    outcome = _outcome(decision["decision_id"])
    route = _route()
    before = deepcopy((decision, outcome, route))
    project_decision_evidence(decision, outcomes=[outcome], route=route)
    assert (decision, outcome, route) == before


def test_module_has_no_operational_dependency_or_io_surface():
    from pathlib import Path
    source = (Path(__file__).parents[1] / "vera" / "minds" /
              "evidence_core.py").read_text(encoding="utf-8")
    for forbidden in (
            "capability_orchestration", "CAPABILITY_REGISTRY", "requests", "httpx",
            "redis", "asyncpg", "sqlite3", "subprocess", "torch", "numpy"):
        assert forbidden not in source
