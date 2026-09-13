from dataclasses import replace
import hashlib

import pytest

from vera.inventory.removal_gate import (
    REQUIRED_EVIDENCE,
    RemovalGateSubmission,
    ZeroUseCycle,
    evaluate_removal_gate,
)


pytestmark = pytest.mark.critical


def digest(value):
    return "sha256:" + hashlib.sha256(str(value).encode()).hexdigest()


def cycle(index=1, **updates):
    values = dict(
        cycle_id=f"normal-cycle-{index}",
        started_at=f"2026-09-{index:02d}T00:00:00Z",
        ended_at=f"2026-09-{index:02d}T23:59:59Z",
        non_probe_calls=0, telemetry_complete=True,
        telemetry_digest=digest(f"cycle-{index}"), normal_operation=True,
    )
    values.update(updates)
    return ZeroUseCycle(**values)


def submission(**updates):
    values = dict(
        candidate_id="cpath_candidate", candidate_name="old.cap",
        replacement="new.cap", owner="vera.owner", review_id="depr_review",
        review_recommendation="removal_candidate",
        evidence_digests={kind: digest(kind) for kind in REQUIRED_EVIDENCE},
        coverage_complete=True, telemetry_complete=True,
        unmigrated_stored_definitions=0,
        zero_use_cycles=(cycle(1), cycle(2)), shadow_disposition="completed",
        conformance_passed=True, quality_regression=False,
        reliability_regression=False, baseline_p95_ms=100,
        candidate_p95_ms=109, performance_exception_justified=False,
        performance_gain_digest="", state_export_checksum_verified=True,
        rollback_retained_for_release=True, documentation_migrated=True,
        explicitly_approved=True, approval_principal="release.owner",
        approval_scope="old.cap", approval_at="2026-09-12T12:00:00Z",
    )
    values.update(updates)
    return RemovalGateSubmission(**values)


def test_complete_evidence_passes_but_gate_never_executes():
    result = evaluate_removal_gate(submission())
    assert result["gate_passed"] is True
    assert result["eligible_for_separately_executed_removal"] is True
    assert result["blockers"] == []
    assert result["removes_anything"] is False
    assert result["executes"] is False and result["mutates"] is False


@pytest.mark.parametrize("kind", sorted(REQUIRED_EVIDENCE))
def test_every_named_evidence_kind_is_mandatory(kind):
    evidence = {item: digest(item) for item in REQUIRED_EVIDENCE if item != kind}
    result = evaluate_removal_gate(submission(evidence_digests=evidence))
    assert result["gate_passed"] is False
    assert f"missing_evidence:{kind}" in result["blockers"]


def test_missing_telemetry_never_counts_as_zero_use():
    result = evaluate_removal_gate(submission(telemetry_complete=False))
    assert "telemetry_incomplete_not_zero_use" in result["blockers"]
    assert result["missing_telemetry_means_zero_use"] is False
    result = evaluate_removal_gate(submission(
        zero_use_cycles=(cycle(1), cycle(2, telemetry_complete=False))))
    assert "cycle_telemetry_incomplete_not_zero_use" in result["blockers"]


def test_two_distinct_normal_cycles_with_zero_non_probe_calls_are_required():
    assert "two_normal_zero_use_cycles_required" in evaluate_removal_gate(
        submission(zero_use_cycles=(cycle(1),)))["blockers"]
    assert "non_normal_observation_cycle" in evaluate_removal_gate(submission(
        zero_use_cycles=(cycle(1), cycle(2, normal_operation=False))))["blockers"]
    assert "non_probe_calls_observed" in evaluate_removal_gate(submission(
        zero_use_cycles=(cycle(1), cycle(2, non_probe_calls=1))))["blockers"]
    with pytest.raises(ValueError, match="unique"):
        submission(zero_use_cycles=(cycle(1), cycle(1)))
    overlapping = (
        cycle(1),
        cycle(2, started_at="2026-09-01T12:00:00Z"),
    )
    assert "observation_cycles_overlap" in evaluate_removal_gate(
        submission(zero_use_cycles=overlapping))["blockers"]


@pytest.mark.parametrize("field,blocker", [
    ("coverage_complete", "inventory_coverage_incomplete"),
    ("conformance_passed", "full_conformance_not_passed"),
    ("quality_regression", "meaningful_quality_regression"),
    ("reliability_regression", "meaningful_reliability_regression"),
    ("state_export_checksum_verified", "state_export_checksum_unverified"),
    ("rollback_retained_for_release", "rollback_not_rehearsed_and_retained"),
    ("documentation_migrated", "documentation_not_migrated"),
    ("explicitly_approved", "explicit_approval_missing"),
])
def test_each_boolean_gate_fails_closed(field, blocker):
    failing = field in {"quality_regression", "reliability_regression"}
    result = evaluate_removal_gate(submission(**{field: failing}))
    assert blocker in result["blockers"]


def test_stored_definitions_and_review_recommendation_block_eligibility():
    result = evaluate_removal_gate(submission(unmigrated_stored_definitions=1))
    assert "unmigrated_stored_definitions_present" in result["blockers"]
    result = evaluate_removal_gate(submission(review_recommendation="retain"))
    assert "independent_review_not_removal_candidate" in result["blockers"]
    result = evaluate_removal_gate(submission(approval_scope="some.other.cap"))
    assert "approval_scope_mismatch" in result["blockers"]


def test_p95_allows_ten_percent_or_a_documented_gain_exception():
    result = evaluate_removal_gate(submission(candidate_p95_ms=111))
    assert "p95_regression_exceeds_ten_percent" in result["blockers"]
    justified = evaluate_removal_gate(submission(
        candidate_p95_ms=111, performance_exception_justified=True,
        performance_gain_digest=digest("documented-gain")))
    assert justified["gate_passed"] is True
    missing_receipt = evaluate_removal_gate(submission(
        candidate_p95_ms=111, performance_exception_justified=True))
    assert "p95_regression_exceeds_ten_percent" in missing_receipt["blockers"]


def test_identity_is_order_stable_and_contains_digests_not_payloads():
    first = evaluate_removal_gate(submission())
    reversed_evidence = dict(reversed(list(submission().evidence_digests.items())))
    second = evaluate_removal_gate(submission(evidence_digests=reversed_evidence))
    assert first == second
    assert first["gate_receipt_id"].startswith("remgate_")
    assert set(first["evidence_digests"]) == REQUIRED_EVIDENCE


def test_malformed_evidence_cycles_and_performance_fail_closed():
    with pytest.raises(ValueError, match="unknown removal evidence"):
        submission(evidence_digests={"approvalish": digest("x")})
    with pytest.raises(ValueError, match="sha256"):
        submission(evidence_digests={"semantic_diff": "raw evidence"})
    with pytest.raises(ValueError, match="positive finite"):
        submission(candidate_p95_ms=float("nan"))
    with pytest.raises(ValueError, match="timezone"):
        cycle(started_at="2026-09-01T00:00:00")
    with pytest.raises(ValueError, match="timezone"):
        submission(approval_at="2026-09-12T12:00:00")
