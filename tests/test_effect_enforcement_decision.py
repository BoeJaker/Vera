import pytest

from vera.integrations.effect_enforcement_decision import (
    DecisionConflict, ExternalEffectEnforcementDecisions)


pytestmark = pytest.mark.critical


def readiness(eligible=True):
    return {"schema": "vera.external-effect-enforcement-readiness/v1",
            "eligible_for_operator_review": eligible,
            "decision": "ready_for_operator_review" if eligible else "collect_more_evidence",
            "unmet_checks": [] if eligible else ["minimum_observations"]}


def test_approval_records_hashed_intent_but_does_not_enable_runtime(tmp_path):
    ledger = ExternalEffectEnforcementDecisions(tmp_path / "decisions.sqlite3")
    result = ledger.decide(
        decision="approve_future_enforcement", expected_revision=0,
        actor_ref="operator:alice", approval_receipt_ref="approval:42",
        readiness=readiness())
    assert result["requested_mode"] == "enforce"
    assert result["effective_mode"] == "observe_only"
    assert result["enforcement_enabled"] is False
    assert "operator:alice" not in str(result)
    assert "approval:42" not in str(result)


def test_continue_observing_reverses_requested_mode(tmp_path):
    ledger = ExternalEffectEnforcementDecisions(tmp_path / "decisions.sqlite3")
    ledger.decide(decision="approve_future_enforcement", expected_revision=0,
                  actor_ref="operator:alice", approval_receipt_ref="approval:42",
                  readiness=readiness())
    result = ledger.decide(decision="continue_observing", expected_revision=1,
                           actor_ref="operator:alice", readiness=readiness(False))
    assert result["revision"] == 2
    assert result["requested_mode"] == "observe_only"
    assert [item["revision"] for item in result["history"]] == [2, 1]


def test_approval_fails_closed_without_readiness_or_receipt(tmp_path):
    ledger = ExternalEffectEnforcementDecisions(tmp_path / "decisions.sqlite3")
    with pytest.raises(ValueError, match="not ready"):
        ledger.decide(decision="approve_future_enforcement", expected_revision=0,
                      actor_ref="operator:alice", approval_receipt_ref="approval:42",
                      readiness=readiness(False))
    with pytest.raises(ValueError, match="approval_receipt_ref"):
        ledger.decide(decision="approve_future_enforcement", expected_revision=0,
                      actor_ref="operator:alice", readiness=readiness())


def test_stale_revision_cannot_overwrite_newer_decision(tmp_path):
    ledger = ExternalEffectEnforcementDecisions(tmp_path / "decisions.sqlite3")
    ledger.decide(decision="continue_observing", expected_revision=0,
                  actor_ref="operator:alice", readiness=readiness(False))
    with pytest.raises(DecisionConflict, match="actual 1"):
        ledger.decide(decision="continue_observing", expected_revision=0,
                      actor_ref="operator:bob", readiness=readiness(False))


@pytest.mark.parametrize("limit", [0, 101, True])
def test_history_window_is_bounded(tmp_path, limit):
    ledger = ExternalEffectEnforcementDecisions(tmp_path / "decisions.sqlite3")
    with pytest.raises(ValueError):
        ledger.current(history_limit=limit)
