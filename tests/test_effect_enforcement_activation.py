import pytest

from vera.integrations.effect_enforcement_activation import (
    ActivationConflict,
    CONTRACT_SHA256,
    ExternalEffectEnforcementActivations,
)


pytestmark = pytest.mark.critical


def approved(revision=3):
    return {"revision": revision, "decision": "approve_future_enforcement"}


def test_activation_requires_gate_current_approval_and_receipt(tmp_path):
    ledger = ExternalEffectEnforcementActivations(tmp_path / "activation.sqlite3")
    with pytest.raises(ValueError, match="gate is disabled"):
        ledger.apply(action="activate", expected_revision=0, decision=approved(),
                     actor_ref="operator:alice", activation_receipt_ref="receipt:1",
                     runtime_gate=False)
    with pytest.raises(ValueError, match="not approved"):
        ledger.apply(action="activate", expected_revision=0,
                     decision={"revision": 1, "decision": "continue_observing"},
                     actor_ref="operator:alice", activation_receipt_ref="receipt:1",
                     runtime_gate=True)
    with pytest.raises(ValueError, match="activation_receipt_ref"):
        ledger.apply(action="activate", expected_revision=0, decision=approved(),
                     actor_ref="operator:alice", runtime_gate=True)


def test_activation_is_contract_bound_reversible_and_payload_free(tmp_path):
    ledger = ExternalEffectEnforcementActivations(tmp_path / "activation.sqlite3")
    active = ledger.apply(
        action="activate", expected_revision=0, decision=approved(),
        actor_ref="operator:alice", activation_receipt_ref="activation:secret",
        runtime_gate=True)
    assert active["enforcement_enabled"] is True
    assert active["effective_mode"] == "enforce"
    assert active["history"][0]["contract_sha256"] == CONTRACT_SHA256
    assert "operator:alice" not in str(active)
    assert "activation:secret" not in str(active)

    assert ledger.current(approved(4), runtime_gate=True)["enforcement_enabled"] is False
    assert ledger.current(approved(), runtime_gate=False)["enforcement_enabled"] is False

    inactive = ledger.apply(
        action="deactivate", expected_revision=1, decision=approved(),
        actor_ref="operator:alice", runtime_gate=True)
    assert inactive["revision"] == 2
    assert inactive["enforcement_enabled"] is False
    assert inactive["requested_active"] is False


def test_activation_uses_optimistic_revision_and_bounded_history(tmp_path):
    ledger = ExternalEffectEnforcementActivations(tmp_path / "activation.sqlite3")
    ledger.apply(action="deactivate", expected_revision=0, decision=approved(),
                 actor_ref="operator:alice", runtime_gate=False)
    with pytest.raises(ActivationConflict, match="expected 0, actual 1"):
        ledger.apply(action="deactivate", expected_revision=0, decision=approved(),
                     actor_ref="operator:bob", runtime_gate=False)
    result = ledger.current(approved(), runtime_gate=False, history_limit=1)
    assert result["window"] == {"requested": 1, "returned": 1}
    with pytest.raises(ValueError, match="between 1 and 100"):
        ledger.current(approved(), runtime_gate=False, history_limit=0)
